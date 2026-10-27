"""2026-10-10 — 처리가 필요한 운영 항목을 화면 기능으로(배포 후 prod DEAD 13건·배포 런북의 CLI 단계).

- 연동 인박스: 조치 안내(hint)·상세(식별자 마스킹)·여러 건 한 번에 종결(전부 아니면 전무)·
  외화 라인 '환율 지정 후 반영'(_erpFxRate·근거·감사·HIS 위조 불가·재처리 시 제거).
- 정기업무: 시드는 누락분만 등록(기존 행=관리자 편집 보존) · 정의 차이 조회 · 선택 반영(감사) · 화면 편집 감사.
- 정합성: 첨부 본문 존재(ATTACHMENT_DIR 없으면 SKIP).
"""
from sqlalchemy import select

from erp.audit.models import AuditLog
from erp.finance.models import Voucher, VoucherLine
from erp.integrations.inbound_consumer import InboundConsumer
from erp.integrations.models import HisInboundEvent
from tests._http import auth_as
from tests.test_inbound_consumer import _seed_corp, _seed_voucher_rules

consumer = InboundConsumer()
BASE = "/api/v1/integrations/his/inbound"


def _dead(ref, event_type="staff.created", error="x", payload=None):
    return HisInboundEvent(domain="finance", event_type=event_type, source_ref=ref,
                           payload=payload or {}, status="DEAD", error=error)


async def _fx_dead(session, ref="voucher:purchase:FXOPS"):
    _seed_voucher_rules(session)
    _seed_corp(session)
    session.add(HisInboundEvent(domain="finance", event_type="voucher.line_recorded", source_ref=ref,
                                payload={"lineType": "purchase", "fromType": "HOSPITAL",
                                         "toDistributorId": "D1", "totalAmount": 2000,
                                         "currency": "USD", "paidAt": "2026-06-18"},
                                status="NEW"))
    await session.flush()
    await consumer.consume(session)
    await session.flush()
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == ref))
    await session.refresh(ev)
    assert ev.status == "DEAD" and "보류" in ev.error, ev.error
    return ev


# ── 조치 안내 ────────────────────────────────────────────────────────────────

def test_action_hint_classifies_prod_dead_shapes():
    from erp.integrations.his_webhook_router import inbound_action_hint as h
    # prod 실측 문구(구 문구 포함) — 화면 버튼 노출이 이 분류에 달려 있다
    assert h("voucher.line_recorded", "voucher:commission:31f2",
             "외화(USD) commission 보류 — 환율(DB-2 라인레벨) 확정 후")["kind"] == "FX_HOLD"
    assert h("voucher.line_recorded", "voucher:refund:d26b",
             "ERP-2003: 외화(USD) refund 보류 — payload에 `fxRate`(거래시점 환율) 미동봉.")["kind"] == "FX_HOLD"
    # 법인계좌 미설정은 보류가 아니라 ERP 설정 문제
    assert h("voucher.line_recorded", "voucher:x", "외화(USD) 법인계좌 미설정 — 등록 필요")["kind"] == "ERP_SETUP"
    assert h("test.ping", "enforce-postrotate-1", "handler 미구현(Wave 후속)")["kind"] == "TEST"
    assert h("staff.created", "hr:probe:1", "employeeNumber 없음")["kind"] == "TEST"
    assert h("staff.created", "tz:realutc:178", "employeeNumber 없음")["kind"] == "TEST"
    assert h("sign.completed", "signcomplete:SR-ERPTEST-1", "erpContractId 누락")["kind"] == "TEST"
    assert h("staff.qualification_changed", "e2e:qual-1", "직원 미상(userId): e2e-user-1")["kind"] == "TEST"
    assert h("staff.created", "staff:EMP-77", "employeeNumber 없음")["kind"] == "SENDER_FIX"
    assert h("staff.qualification_changed", "staff:q:9", "직원 미상(userId): u9")["kind"] == "STAFF_UNKNOWN"
    assert h("claim.reviewed", "claim:review:1",
             "ERP-2003: 삭감이 줄어든 HIS 재심사(revision 없음)는 …")["kind"] == "RESTORE_HOLD"
    assert h("billing.recorded", "b:1", "알 수 없는 오류")["kind"] == "OTHER"
    # 토큰 단위 — UUID 안의 우연한 'e2e'·'test' 부분 문자열은 테스트로 보지 않는다(실거래 일괄 종결 방지)
    assert h("staff.created", "staff:3fa2e2e1-9c1d-4e2e-a1b2-c3d4e5f60789", "employeeNumber 없음")["kind"] == "SENDER_FIX"
    assert h("staff.created", "staff:latest-77", "employeeNumber 없음")["kind"] == "SENDER_FIX"
    assert h("voucher.line_recorded", "voucher:sale:1", "외화(USD) sale 부킹 보류")["kind"] == "FX_HOLD"
    assert h("voucher.line_recorded", "voucher::1", "외화(USD)  보류 — x")["kind"] == "FX_HOLD"


async def test_list_carries_hint_and_detail_masks_identifiers(client, session):
    admin = await auth_as(session, user_id=601, role="ADMIN")
    staff = await auth_as(session, user_id=602, role="STAFF")
    ev = _dead("sign:ops:1", "sign.completed", "erpContractId 누락", payload={
        "patientId": "P-123456", "certSerial": "ABCDEF", "tsaToken": "t" * 500, "status": "DONE",
        "document": {"signerName": "홍길동", "pages": 3}, "lineType": "n" * 300, "note": "자유 기술",
        "visitId": "V-1", "items": [{"testName": "CBC", "code": "B1"}],
        "_erpFxRate": {"rate": 1, "by": 1, "reason": "근거"}})
    session.add(ev)
    await session.flush()
    r = await client.get(f"{BASE}?status=DEAD", headers=admin)
    row = next(i for i in r.json()["items"] if i["id"] == ev.id)
    assert row["hint"]["kind"] == "SENDER_FIX"
    assert (await client.get(f"{BASE}/{ev.id}", headers=staff)).status_code == 403
    d = (await client.get(f"{BASE}/{ev.id}", headers=admin)).json()
    p = d["payload"]
    assert p["patientId"] == "***(8자)" and p["certSerial"].startswith("***")
    assert p["tsaToken"].startswith("***") and p["document"]["signerName"] == "***(3자)"
    assert p["status"] == "DONE" and p["document"]["pages"] == 3
    assert p["lineType"].endswith("…") and len(p["lineType"]) == 201     # 긴 값은 절단
    assert p["note"].startswith("***") and p["visitId"].startswith("***")   # 자유 기술·방문 id
    assert p["items"][0] == {"testName": "***(3자)", "code": "B1"}            # 목록 안도 같은 규칙
    assert p["_erpFxRate"] == {"rate": 1, "by": 1, "reason": "근거"}          # ERP 조치 기록은 그대로
    assert (await client.get(f"{BASE}/999999", headers=admin)).status_code == 422


# ── 여러 건 종결 ─────────────────────────────────────────────────────────────

async def test_bulk_resolve_all_or_nothing_with_per_event_audit(client, session):
    admin = await auth_as(session, user_id=603, role="ADMIN")
    a, b = _dead("bulk:test:1", "test.ping"), _dead("bulk:test:2", "test.ping")
    failed = HisInboundEvent(domain="finance", event_type="test.ping", source_ref="bulk:test:3",
                             payload={}, status="FAILED", error="일시")
    session.add_all([a, b, failed])
    await session.flush()
    url = f"{BASE}/resolve-bulk"
    r = await client.post(url, headers=admin, json={"ids": [a.id, failed.id], "reason": "점검 이벤트"})
    assert r.status_code == 422 and "FAILED" in r.text
    await session.refresh(a)
    assert a.status == "DEAD"                                          # 하나도 종결되지 않음
    r = await client.post(url, headers=admin, json={"ids": [a.id, 999999], "reason": "x"})
    assert r.status_code == 422 and "999999" in r.text
    assert (await client.post(url, headers=admin, json={"ids": [a.id], "reason": "  "})).status_code == 422
    assert (await client.post(url, headers=admin, json={"ids": [], "reason": "x"})).status_code == 422
    r = await client.post(url, headers=admin, json={"ids": [b.id, a.id, a.id], "reason": "연동 점검 이벤트"})
    assert r.status_code == 200 and r.json()["resolved"] == sorted([a.id, b.id])
    for ev in (a, b):
        await session.refresh(ev)
        assert ev.status == "RESOLVED" and ev.resolve_reason == "연동 점검 이벤트" and ev.resolved_by == 603
    n = (await session.scalars(select(AuditLog).where(
        AuditLog.action == "RESOLVE", AuditLog.target_id.in_([str(a.id), str(b.id)])))).all()
    assert len(n) == 2


# ── 환율 지정 후 반영 ────────────────────────────────────────────────────────

async def test_apply_fx_gate_posts_with_operator_rate_and_audit(client, session):
    admin = await auth_as(session, user_id=604, role="ADMIN")
    staff = await auth_as(session, user_id=605, role="STAFF")
    ev = await _fx_dead(session)
    other = _dead("fx:other", "staff.created", "employeeNumber 없음")
    session.add(other)
    await session.flush()
    url = f"{BASE}/{ev.id}/apply-fx"
    body = {"fx_rate": 1385.5, "reason": "서울외국환중개 2026-06-18 매매기준율"}
    assert (await client.post(url, headers=staff, json=body)).status_code == 403
    for bad in ({"fx_rate": 0, "reason": "x"}, {"fx_rate": -1, "reason": "x"},
                {"fx_rate": 1e6, "reason": "x"}, {"fx_rate": 1385, "reason": "  "}, {"fx_rate": 1385}):
        assert (await client.post(url, headers=admin, json=bad)).status_code == 422, bad
    r = await client.post(f"{BASE}/{other.id}/apply-fx", headers=admin, json=body)
    assert r.status_code == 422 and "외화" in r.text
    r = await client.post(url, headers=admin, json=body)
    assert r.status_code == 200, r.text
    await session.refresh(ev)
    assert ev.status == "NEW" and ev.payload["_erpFxRate"]["rate"] == 1385.5
    assert ev.payload["_erpFxRate"]["by"] == 604 and ev.payload["_erpFxRate"]["currency"] == "USD"
    audit = await session.scalar(select(AuditLog).where(
        AuditLog.action == "FX_APPLY", AuditLog.target_id == str(ev.id)))
    assert audit and audit.after["fx_rate"] == 1385.5
    await consumer.consume(session)
    await session.flush()
    await session.refresh(ev)
    assert ev.status == "APPLIED", ev.error
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == ev.source_ref))
    dr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    assert dr.account_code == "1115" and float(dr.amount) == round(2000 * 1385.5)
    assert "ERP 지정 #604" in (v.memo or "")                           # 전표에 출처 표기
    # APPLIED 뒤에는 다시 지정할 수 없다(이중 전기 방지)
    assert (await client.post(url, headers=admin, json=body)).status_code == 422


async def test_requeue_drops_operator_fx_rate(client, session):
    admin = await auth_as(session, user_id=606, role="ADMIN")
    ev = await _fx_dead(session, "voucher:purchase:FXREQ")
    ev.payload = {**ev.payload, "_erpFxRate": {"rate": 1300, "by": 1}, "_erpConfirmedRestore": {"by": 1}}
    await session.flush()
    r = await client.post(f"{BASE}/{ev.id}/requeue", headers=admin)
    assert r.status_code == 200
    await session.refresh(ev)
    assert not [k for k in ev.payload if k.startswith("_erp")]
    await consumer.consume(session)
    await session.flush()
    await session.refresh(ev)
    assert ev.status == "DEAD" and "보류" in ev.error                  # 지정이 지워져 다시 보류


async def test_his_fx_rate_wins_over_operator_rate(session):
    """HIS가 fxRate를 동봉하면 그것이 우선 — 운영자 지정은 미동봉일 때만 쓰인다."""
    from erp.integrations.inbound_consumer import _effective_fx_rate
    assert _effective_fx_rate({"fxRate": 1350, "_erpFxRate": {"rate": 1, "by": 9}})[0] == 1350
    assert _effective_fx_rate({"_erpFxRate": {"rate": 1400, "by": 9}}) == (1400.0, "1400(ERP 지정 #9)")
    assert _effective_fx_rate({}) == (None, None)


async def test_lis_webhook_strips_erp_reserved_keys(client, session, monkeypatch):
    import hashlib
    import hmac
    import json
    import time

    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "lis_billing_secret", "lis-secret-ops")
    body = json.dumps({"lisBillingId": "LIS-OPS-1", "revision": 1, "_erpFxRate": {"rate": 9},
                       "items": []}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(b"lis-secret-ops", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    r = await client.post("/api/v1/integration/lis/billing", content=body, headers={
        "x-integration-signature": sig, "x-integration-ts": ts, "content-type": "application/json"})
    assert r.status_code == 202, r.text
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == "lis:LIS-OPS-1:1"))
    assert ev is not None and "_erpFxRate" not in ev.payload


# ── 정기업무 정의 동기화 ─────────────────────────────────────────────────────

async def test_seed_preserves_admin_edits_and_drift_reports_them(client, session):
    from erp.app.recurring_service import RecurringRunner
    runner = RecurringRunner()
    admin = await auth_as(session, user_id=607, role="ADMIN")
    await runner.seed_starter_tasks(session)
    await runner.seed_migrated_jobs(session)
    d = (await client.get("/api/v1/admin/recurring-tasks/registry/drift", headers=admin)).json()
    assert d["in_sync"] is True and d["missing"] == [] and d["differs"] == []
    # 관리자가 화면에서 담당 역할 변경(감사) → 시드 재실행이 되돌리지 않는다(종전엔 덮어씀)
    r = await client.put("/api/v1/admin/recurring-tasks/ops.ar_review_weekly", headers=admin,
                         json={"owner_role": "ADMIN", "sla_days": 4})
    assert r.status_code == 200
    a = await session.scalar(select(AuditLog).where(
        AuditLog.action == "UPSERT", AuditLog.target_id == "ops.ar_review_weekly"))
    assert a.before == {"owner_role": "FINANCE_MANAGER", "sla_days": 3}
    assert a.after == {"owner_role": "ADMIN", "sla_days": 4}
    await runner.seed_starter_tasks(session)
    t = next(x for x in await runner.list_tasks(session) if x["code"] == "ops.ar_review_weekly")
    assert t["owner_role"] == "ADMIN" and t["sla_days"] == 4
    # 차이 조회 — 관리자가 고를 수 있게 필드별로
    d = (await client.get("/api/v1/admin/recurring-tasks/registry/drift", headers=admin)).json()
    x = next(i for i in d["differs"] if i["code"] == "ops.ar_review_weekly")
    assert {f["field"]: (f["registry"], f["code"]) for f in x["fields"]} == {
        "owner_role": ("ADMIN", "FINANCE_MANAGER"), "sla_days": (4, 3)}
    assert d["in_sync"] is False


async def test_registry_sync_adds_missing_and_applies_code_values_keeping_enabled(client, session):
    from sqlalchemy import delete

    from erp.app.recurring_service import RecurringRunner
    from erp.common.models import RecurringTask
    runner = RecurringRunner()
    admin = await auth_as(session, user_id=608, role="ADMIN")
    staff = await auth_as(session, user_id=609, role="STAFF")
    await runner.seed_starter_tasks(session)
    await runner.seed_migrated_jobs(session)
    await session.execute(delete(RecurringTask).where(RecurringTask.code == "hr.license_expiry"))
    await runner.upsert_task(session, code="patient.accrual", actor=1, enabled=False, sla_days=9)
    url = "/api/v1/admin/recurring-tasks/registry/sync"
    assert (await client.post(url, headers=staff, json={"codes": ["patient.accrual"]})).status_code == 403
    r = await client.post(url, headers=admin, json={"codes": ["nope.task"]})
    assert r.status_code == 422 and "nope.task" in r.text
    d = (await client.get("/api/v1/admin/recurring-tasks/registry/drift", headers=admin)).json()
    assert [m["code"] for m in d["missing"]] == ["hr.license_expiry"]
    r = await client.post(url, headers=admin, json={"codes": ["hr.license_expiry", "patient.accrual",
                                                              "finance.ledger_integrity"]})
    assert r.status_code == 200
    assert r.json()["applied"] == ["hr.license_expiry", "patient.accrual"]   # 이미 일치하면 건너뜀
    tasks = {t["code"]: t for t in await runner.list_tasks(session)}
    assert tasks["hr.license_expiry"]["enabled"] is True
    assert tasks["patient.accrual"]["sla_days"] == 1 and tasks["patient.accrual"]["enabled"] is False
    audits = (await session.scalars(select(AuditLog).where(AuditLog.action == "REGISTRY_SYNC"))).all()
    assert {a.target_id for a in audits} == {"hr.license_expiry", "patient.accrual"}
    assert next(a for a in audits if a.target_id == "hr.license_expiry").before is None


# ── 정합성: 첨부 본문 ────────────────────────────────────────────────────────

async def test_consistency_attachment_body_check(session, tmp_path, monkeypatch):
    from erp.app import consistency_service as cs
    from erp.common.models import Attachment
    from erp.config import get_settings
    fn = next(f for k, _t, _d, f in cs._CHECKS if k == "attachments.body_present")
    monkeypatch.setattr(get_settings(), "attachment_dir", "")
    assert (await fn(session))["status"] == "SKIP"
    monkeypatch.setattr(get_settings(), "attachment_dir", str(tmp_path))
    assert (await fn(session))["status"] == "OK"
    session.add(Attachment(entity_type="voucher", entity_id=1, filename="a.pdf", sha256="0" * 64,
                           size_bytes=1, content_type="application/pdf", uploaded_by=1))
    await session.flush()
    r = await fn(session)
    assert r["status"] == "FAIL" and r["detail"][0]["entity_type"] == "voucher" and "재발급" in r["action"]
    assert "filename" not in r["detail"][0]                            # 실명이 들어갈 수 있는 파일명은 싣지 않음


async def test_registry_apply_clears_handler_and_keep_hides_until_code_changes(client, session, monkeypatch):
    """MANUAL 정의(handler_ref=None)로 맞추면 핸들러도 지워져 차이가 닫힌다 · '현재 값 유지'는 그 코드 정의에만 유효."""
    from erp.app.recurring_service import RecurringRunner
    runner = RecurringRunner()
    admin = await auth_as(session, user_id=610, role="ADMIN")
    await runner.seed_starter_tasks(session)
    await runner.seed_migrated_jobs(session)
    await runner.upsert_task(session, code="hr.withholding_tax", actor=1, execution_mode="AUTO",
                             handler_ref="hr.license_expiry")
    r = await client.post("/api/v1/admin/recurring-tasks/registry/sync", headers=admin,
                          json={"codes": ["hr.withholding_tax"]})
    assert r.json()["applied"] == ["hr.withholding_tax"]
    d = await runner.registry_drift(session)
    assert d["in_sync"] is True                                        # handler_ref도 None으로
    # 의도된 편집 유지
    await runner.upsert_task(session, code="hr.withholding_tax", actor=1, owner_role="FINANCE_MANAGER")
    url = "/api/v1/admin/recurring-tasks/registry/hr.withholding_tax/keep"
    assert (await client.post(url, headers=admin, json={"reason": " "})).status_code == 422
    r = await client.post(url, headers=admin, json={"reason": "재무팀 담당으로 이관(운영회의)"})
    assert r.status_code == 200
    d = await runner.registry_drift(session)
    assert d["in_sync"] is True and d["kept"] == ["hr.withholding_tax"]
    # 코드 정의가 바뀌면 다시 보인다
    orig = RecurringRunner._STARTER
    monkeypatch.setattr(RecurringRunner, "_STARTER", [
        (c[0], c[1], c[2], c[3], c[4], c[5], (9 if c[0] == "hr.withholding_tax" else c[6]), c[7])
        for c in orig])
    d = await runner.registry_drift(session)
    assert [x["code"] for x in d["differs"]] == ["hr.withholding_tax"] and d["kept"] == []


def test_mask_token_rules_subtrees_and_erp_depth():
    from erp.integrations.his_webhook_router import _mask_payload
    m = _mask_payload({
        "patient": {"id": "P1", "age": 34}, "visit": ["V1"], "temperature": 36.5, "hotelCode": "H",
        "debitAccount": "1110", "accountNo": "110-123", "taxId": "123-45", "exemptFlag": True,
        "items": [{"_erpNote": "홍길동", "code": "B1"}], "_erpFxRate": {"rate": 1, "reason": "근거"}})
    assert m["patient"] == "***(dict 2개)" and m["visit"] == ["***(2자)"]   # 민감 키 아래는 통째로
    # 허용 목록(fail-closed) — 모르는 키는 값이 가려진다(차단 목록을 4회 넓혀도 누락이 이어져 전환)
    assert m["temperature"].startswith("***") and m["exemptFlag"].startswith("***")
    assert m["hotelCode"].startswith("***") and m["debitAccount"] == "1110"   # 모르는 단어 포함=가림·계정은 판단 근거
    assert m["accountNo"].startswith("***") and m["taxId"].startswith("***")
    assert m["items"][0]["_erpNote"] == "***(3자)" and m["items"][0]["code"] == "B1"
    assert m["_erpFxRate"] == {"rate": 1, "reason": "근거"}
    # 실제 prod DEAD 외화 라인 형태 — 조치 판단에 필요한 값은 모두 보인다
    fx = _mask_payload({"fromId": "H1", "source": "his", "currency": "USD", "fromType": "HOSPITAL",
                        "lineType": "purchase", "quantity": 3, "supplyPrice": 10, "totalAmount": 30,
                        "allocationId": "A", "toDealerType": "OVERSEAS", "toDistributorId": "D",
                        "paidAt": "2026-06-18", "commissionRate": 0.1,
                        "dealerName": "딜러", "serial": "S"})
    assert fx.pop("dealerName").startswith("***") and not [k for k, v in fx.items()
                                                           if isinstance(v, str) and v.startswith("***")]
    assert _mask_payload({"externalSignerRef": "erpcontract:medical_contract:7"})["externalSignerRef"] \
        == "erpcontract:medical_contract:7"
    assert _mask_payload({"document": {"pages": 2}})["document"] == {"pages": 2}
    assert _mask_payload({"externalSignerRef": "erpcontract:9 홍길동"})["externalSignerRef"].startswith("***")
    assert _mask_payload({"x": {"externalSignerRef": "erpcontract:a:1"}})["x"]["externalSignerRef"].startswith("***")
    lm = _mask_payload({"lineItems": ["홍"], "orderItems": [{"code": "B1"}], "testItems": "홍",
                        "codes": ["A1"]})
    assert lm["lineItems"] == ["***(1자)"] and lm["orderItems"] == [{"code": "B1"}]
    assert lm["testItems"].startswith("***") and lm["codes"] == ["***(2자)"]
    claim = _mask_payload({"claimId": "c", "claimNumber": "n", "status": "S", "approvedAmount": 1,
                           "reducedAmount": 2, "revision": 1, "careMonth": "2026-09", "lisBillingId": "L",
                           "reductionReasonCode": "A1", "reductionReason": "자유 기술", "netPayableKrw": 3,
                           "fxRate": 1350, "settlementId": "s", "debitAccount": "1110"})
    assert claim.pop("reductionReason").startswith("***")
    assert not [k for k, v in claim.items() if isinstance(v, str) and v.startswith("***")], claim
    for k in ("encounterid", "admissionno", "diagcode", "userid", "cardno", "insuranceNo", "guardian",
              "firstname", "remarks", "chartOfAccounts", "policyNo", "accountHolder", "contractParty",
              "dealerContact", "paymentPayee", "documentTitle", "replyTo", "kcdCode", "icd10Code",
              "diseaseCode", "id", "approvedBy", "reviewedBy", "decidedBy", "sourceRef", "paymentRef",
              "document", "externalSignerRef"):
        assert _mask_payload({k: "v"})[k].startswith("***"), k
    low = _mask_payload({"patientid": "P1", "chartno": "C1", "birthdate": "2000", "phonenumber": "010",
                         "visitid": "V", "empno": "E", "fullname": "홍", "ptNm": "홍", "patNo": "1",
                         "custNm": "김", "hpNo": "010", "juminNo": "9", "telNo": "02", "status": "OK"})
    assert all(v.startswith("***") for k, v in low.items() if k != "status") and low["status"] == "OK"


async def test_keep_reappears_when_registry_changes_again(client, session):
    from erp.app.recurring_service import RecurringRunner
    runner = RecurringRunner()
    admin = await auth_as(session, user_id=611, role="ADMIN")
    await runner.seed_starter_tasks(session)
    await runner.seed_migrated_jobs(session)
    await runner.upsert_task(session, code="hr.withholding_tax", actor=1, owner_role="FINANCE_MANAGER")
    r = await client.post("/api/v1/admin/recurring-tasks/registry/hr.withholding_tax/keep",
                          headers=admin, json={"reason": "의도된 담당"})
    assert r.status_code == 200 and (await runner.registry_drift(session))["in_sync"] is True
    await runner.upsert_task(session, code="hr.withholding_tax", actor=1, sla_days=11)   # 확인 안 된 새 편집
    d = await runner.registry_drift(session)
    assert [x["code"] for x in d["differs"]] == ["hr.withholding_tax"] and d["kept"] == []


async def test_missing_fx_account_surfaces_before_rate_hold(session):
    """법인계좌가 없으면 환율 보류보다 먼저 ERP 설정으로 — 환율을 넣기 전에 계좌 문제가 보인다."""
    _seed_voucher_rules(session)
    session.add(HisInboundEvent(domain="finance", event_type="voucher.line_recorded",
                                source_ref="voucher:purchase:NOACCT", status="NEW",
                                payload={"lineType": "purchase", "totalAmount": 10, "currency": "USD"}))
    await session.flush()
    await consumer.consume(session)
    await session.flush()
    ev = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == "voucher:purchase:NOACCT"))
    await session.refresh(ev)
    from erp.integrations.his_webhook_router import inbound_action_hint
    assert ev.status == "DEAD" and "법인계좌" in ev.error
    assert inbound_action_hint(ev.event_type, ev.source_ref, ev.error)["kind"] == "ERP_SETUP"
