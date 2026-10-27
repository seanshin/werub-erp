"""Phase0 가명 토큰(patientRef) — 토큰·지문·대사표 이관·역해소·컷오버 consumer 통합."""
from sqlalchemy import select

from erp.config import get_settings
from erp.integrations import patient_ref as pr
from erp.integrations.inbound_consumer import InboundConsumer
from erp.integrations.models import HisInboundEvent, PendingPatientRef

consumer = InboundConsumer()
KEY = "test-pseudonym-key-0123456789"


def test_token_deterministic_and_16hex():
    t1 = pr.token_for("C000123", KEY)
    t2 = pr.token_for("C000123", KEY)
    assert t1 == t2 and len(t1) == 16
    assert all(c in "0123456789abcdef" for c in t1)
    assert pr.token_for("C000124", KEY) != t1        # 환자별 상이
    assert pr.token_for("C000123", "other") != t1     # 키별 상이


def test_key_fingerprint_len12():
    fp = pr.key_fingerprint(KEY)
    assert len(fp) == 12 and all(c in "0123456789abcdef" for c in fp)


async def test_ingest_verify_and_resolve(session):
    rows = [("C001", pr.token_for("C001", KEY)),
            ("C002", pr.token_for("C002", KEY))]
    out = await pr.ingest_map(session, rows, key=KEY, verify=True)
    assert out["ingested"] == 2 and out["mismatched"] == 0 and out["total_mapped"] == 2
    assert await pr.resolve(session, pr.token_for("C001", KEY)) == "C001"
    assert await pr.resolve(session, "nope") is None
    # 멱등 재이관 — 신규 0·중복 무증가
    out2 = await pr.ingest_map(session, rows, key=KEY, verify=True)
    assert out2["ingested"] == 0 and out2["total_mapped"] == 2


async def test_ingest_rejects_mismatch(session):
    # 잘못된 patientRef(키 불일치) → 이관 제외·mismatched 보고(원본 오염 차단)
    rows = [("C010", pr.token_for("C010", KEY)),          # 정상
            ("C011", "deadbeefdeadbeef")]                 # 위조/구키
    out = await pr.ingest_map(session, rows, key=KEY, verify=True)
    assert out["ingested"] == 1 and out["mismatched"] == 1
    assert "C011" in out.get("mismatch_chart_samples", [])
    assert await pr.resolve(session, "deadbeefdeadbeef") is None


async def test_resolve_inbound_gate_off_passthrough(session):
    s = get_settings()
    old = s.his_pseudonym_enabled
    try:
        s.his_pseudonym_enabled = False
        assert await pr.resolve_inbound_chart(session, "C999") == "C999"   # 무변경
    finally:
        s.his_pseudonym_enabled = old


async def test_resolve_inbound_gate_on_resolves_and_pending(session):
    s = get_settings()
    old = s.his_pseudonym_enabled
    try:
        s.his_pseudonym_enabled = True
        await pr.ingest_map(session, [("C777", pr.token_for("C777", KEY))], key=KEY)
        assert await pr.resolve_inbound_chart(session, pr.token_for("C777", KEY)) == "C777"
        # 미등록 → PendingRefError
        try:
            await pr.resolve_inbound_chart(session, "unregistered16hx", event_type="billing.line_recorded")
            raise AssertionError("PendingRefError 미발생")
        except pr.PendingRefError as e:
            assert e.patient_ref == "unregistered16hx"
    finally:
        s.his_pseudonym_enabled = old


async def test_consumer_cutover_resolves_line(session):
    """컷오버 ON: line_recorded의 chartNo=patientRef → 원 chart_no로 역해소 저장."""
    from erp.patient.models import BillingLine
    s = get_settings()
    old = s.his_pseudonym_enabled
    try:
        s.his_pseudonym_enabled = True
        await pr.ingest_map(session, [("CHART-55", pr.token_for("CHART-55", KEY))], key=KEY)
        ref = pr.token_for("CHART-55", KEY)
        session.add(HisInboundEvent(domain="finance", event_type="billing.line_recorded",
                                    source_ref="EVT-PR1", status="NEW",
                                    payload={"lineId": "L1", "chartNo": ref, "code": "AA100",
                                             "amount": 10000, "treatedOn": "2026-07-01"}))
        await session.flush()
        out = await consumer.consume(session)
        assert out["applied"] == 1
        row = await session.scalar(select(BillingLine).where(BillingLine.line_id == "L1"))
        assert row.chart_no == "CHART-55"     # patientRef가 아니라 원 chart_no로 저장
    finally:
        s.his_pseudonym_enabled = old


async def test_consumer_cutover_unregistered_holds_pending(session):
    """컷오버 ON·미등록 patientRef → FAILED 보류 + pending 큐 적재(DEAD 아님)."""
    s = get_settings()
    old = s.his_pseudonym_enabled
    try:
        s.his_pseudonym_enabled = True
        session.add(HisInboundEvent(domain="finance", event_type="billing.line_recorded",
                                    source_ref="EVT-PR2", status="NEW",
                                    payload={"lineId": "L2", "chartNo": "abcdef0123456789",
                                             "code": "AA100", "amount": 10000,
                                             "treatedOn": "2026-07-01"}))
        await session.flush()
        out = await consumer.consume(session)
        assert out["failed"] == 1
        ev = await session.scalar(select(HisInboundEvent).where(
            HisInboundEvent.source_ref == "EVT-PR2"))
        assert ev.status == "FAILED" and ev.retry_count == 0     # 보류(무증가)·DEAD 아님
        pend = await session.scalar(select(PendingPatientRef).where(
            PendingPatientRef.patient_ref == "abcdef0123456789"))
        assert pend is not None and pend.event_count == 1
    finally:
        s.his_pseudonym_enabled = old


async def _org_row(session):
    from erp.common.models import OrgSettings
    session.add(OrgSettings(id=1, name="테스트병원"))
    await session.flush()


async def test_cutover_runtime_db_flag(session):
    """런타임 DB 플래그로 컷오버 토글(env OFF·무재시작) — resolve_inbound_chart 반영."""
    from erp.common.org_service import OrgSettingsService
    from erp.integrations.patient_ref import cutover_enabled, resolve_inbound_chart
    s = get_settings()
    old = s.his_pseudonym_enabled
    try:
        s.his_pseudonym_enabled = False          # env OFF
        await _org_row(session)
        assert await cutover_enabled(session) is False
        assert await resolve_inbound_chart(session, "C123") == "C123"   # OFF=passthrough
        # DB 토글 ON → 게이트 활성(env 무변경)
        await OrgSettingsService().set_phase0_cutover(session, enabled=True, actor=1)
        assert await cutover_enabled(session) is True
        await pr.ingest_map(session, [("C123", pr.token_for("C123", KEY))], key=KEY)
        assert await resolve_inbound_chart(session, pr.token_for("C123", KEY)) == "C123"
        # 롤백 즉시
        await OrgSettingsService().set_phase0_cutover(session, enabled=False, actor=1)
        assert await cutover_enabled(session) is False
    finally:
        s.his_pseudonym_enabled = old


async def test_cutover_env_overrides_db(session):
    """env ON이면 DB 플래그 무관 활성(fail-safe 레거시 경로)."""
    from erp.integrations.patient_ref import cutover_enabled
    s = get_settings()
    old = s.his_pseudonym_enabled
    try:
        s.his_pseudonym_enabled = True
        await _org_row(session)   # DB flag False
        assert await cutover_enabled(session) is True   # env OR db → True
    finally:
        s.his_pseudonym_enabled = old
