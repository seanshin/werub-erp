"""2026-10-09 시나리오 3회차 — HIS 재심사 추론 정정의 이의 가드 범위·출구 없는 DEAD·낡은 재생·청구 잠금.

1. 이의 가드: 진료월 전체(미상이면 전 이력)를 보던 것을 '이 청구의 직전 결정 이후 종결'로 한정.
   HIS claim.submitted는 careMonth를 싣지 않는다 — care_month NULL이 정상 경로.
2. DEAD 종결 출구: POST /integrations/his/inbound/{id}/resolve(사유 필수·DEAD만·감사).
3. 낡은 재생: 같은 청구의 더 늦게 수신된(인박스 id 큰) 심사가 이미 APPLIED면 덮지 않는다.
4. 청구 advisory 잠금 비차단(PG 레인): 다른 트랜잭션이 쥐고 있으면 FAILED·재시도(retry_count 무증가).
"""
import asyncio
import os
import uuid
import zlib
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select, text, update

from erp.claims.models import ClaimAdjustment, ClaimBatch, ClaimStatement, ClaimSubmission
from erp.claims.service import AppealService
from erp.finance.models import Voucher, VoucherLine
from erp.integrations import inbound_consumer as ic
from erp.integrations.inbound_consumer import InboundConsumer
from erp.integrations.models import HisInboundEvent
from tests._http import auth_as

consumer = InboundConsumer()
TODAY = date.today()
_PG_URL = os.environ.get("TEST_DATABASE_URL")
_HINT = "처리 완료(사유)"


async def _gl(session, acct):
    d = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.account_code == acct, VoucherLine.side == "DEBIT"))
    c = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.account_code == acct, VoucherLine.side == "CREDIT"))
    return float(d) - float(c)


async def _push(session, event_type, ref, payload) -> HisInboundEvent:
    session.add(HisInboundEvent(domain="finance", event_type=event_type, source_ref=ref,
                                payload=payload, status="NEW"))
    await session.flush()
    await consumer.consume(session)
    return await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == ref))


async def _submit(session, cid, amt, care_month=None):
    """HIS 실 형태 — careMonth 미동봉(his-erp-webhook-payload-schemas.md)."""
    p = {"claimId": cid, "claimNumber": f"CLM-{cid[:8]}", "claimedAmount": amt}
    if care_month:
        p["careMonth"] = care_month
    ev = await _push(session, "claim.submitted", f"claim:submit:{cid}", p)
    assert ev.status == "APPLIED", ev.error


async def _review(session, cid, status, approved, reduced, **extra):
    ref = f"claim:review:{cid}:{status}:{approved}:{reduced}"
    return await _push(session, "claim.reviewed", ref, {
        "claimId": cid, "claimNumber": f"CLM-{cid[:8]}", "status": status,
        "approvedAmount": approved, "reducedAmount": reduced, **extra})


async def _sub(session, cid):
    return await session.scalar(select(ClaimSubmission).where(ClaimSubmission.claim_id == cid))


async def _confirm(session, ev) -> HisInboundEvent:
    """2026-10-09 시나리오 5회차: 삭감 감소 재심사는 보류(DEAD) → 운영자 '확인 후 반영'(표식 동봉 재처리)."""
    assert ev.status == "DEAD" and "확인 후 반영" in (ev.error or ""), ev.error
    ev.payload = {**ev.payload, "_erpConfirmedRestore": {"by": 1, "reason": "이의 결과 대조"}}
    ev.status, ev.error, ev.retry_count = "NEW", None, 0
    await session.flush()
    await consumer.consume(session)
    await session.flush()
    await session.refresh(ev)
    return ev


async def _closed_appeal(session, care_month, closed_on: date):
    """이의 인정 종결(DR1140/CR5910 10만) — 종결 전표 일자를 closed_on으로(과거 종결 재현)."""
    b = ClaimBatch(receipt_no=f"R-R3-{uuid.uuid4().hex[:8]}", care_month=care_month,
                   notice_arrived_on=closed_on - timedelta(days=10))
    session.add(b)
    await session.flush()
    st = ClaimStatement(batch_id=b.id, stmt_serial="001", chart_no="C-R3", dept_code="IM",
                        claimed_amt=1_000_000, decided_amt=900_000, adjusted_amt=100_000)
    session.add(st)
    await session.flush()
    adj = ClaimAdjustment(statement_id=st.id, adj_amt=100_000, map_status="AUTO")
    session.add(adj)
    await session.flush()
    svc = AppealService()
    a = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                       notice_arrived_on=closed_on - timedelta(days=5), actor=1)
    await svc.transition(session, appeal_id=a.id, status="ACCEPTED", actor=1)
    await session.execute(update(Voucher).where(Voucher.source_module == "claims",
                                                Voucher.source_ref == f"appeal-adj-{a.id}")
                          .values(voucher_on=closed_on))
    await session.flush()
    return a


# ── 1. 이의 가드 범위 ────────────────────────────────────────────────────────

async def test_null_care_month_unrelated_old_appeal_confirmed_rereview_applies(session):
    """MED — care_month NULL(정상 경로)에서 무관한 **옛** 이의 종결이 있어도, 운영자가 확인 후 반영하면
    이 청구의 삭감만 역분개된다(5회차: 복원 방향은 이의 유무와 무관하게 확인 보류 → 확인 후 적용)."""
    await _closed_appeal(session, "2026-01", TODAY - timedelta(days=40))
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    assert (await _sub(session, cid)).care_month is None
    ev1 = await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    assert ev1.status == "APPLIED", ev1.error
    ev2 = await _confirm(session, await _review(session, cid, "APPROVED", 500_000, 0))
    assert ev2.status == "APPLIED", ev2.error
    # 옛 이의의 CR5910이 GL에 섞여 있어 이 청구의 심사 전표 체인으로 확인
    loss = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).join(
        Voucher, Voucher.id == VoucherLine.voucher_id).where(
        Voucher.source_module == "his.claim", VoucherLine.account_code == "5910",
        VoucherLine.side == "DEBIT"))
    rev = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).join(
        Voucher, Voucher.id == VoucherLine.voucher_id).where(
        Voucher.source_module == "his.claim", VoucherLine.account_code == "5910",
        VoucherLine.side == "CREDIT"))
    assert float(loss) - float(rev) == 0                     # 이 청구의 삭감 순액 0(역분개 적용)
    sub = await _sub(session, cid)
    assert float(sub.decided_amt) == 500_000 and sub.decided_rev == 2


async def test_null_care_month_appeal_closed_after_decision_goes_dead_with_resolve_hint(session):
    """care_month NULL이어도 이 청구의 결정 **이후** 종결된 이의는 여전히 DEAD — 메시지는 실제 출구를 가리킨다."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000,
                  reviewedAt=(TODAY - timedelta(days=10)).isoformat())
    await _closed_appeal(session, "2026-02", TODAY - timedelta(days=3))
    ev = await _review(session, cid, "APPROVED", 500_000, 0)
    assert ev.status == "DEAD"
    assert "이의신청" in ev.error and _HINT in ev.error and "진료월의" not in ev.error
    assert float((await _sub(session, cid)).decided_amt) == 400_000


async def test_same_month_appeal_closed_before_decision_confirmed_applies(session):
    """진료월을 알고 그 달 이의가 결정 이전에 종결됐어도 복원 방향은 보류(5회차) — 확인 후 반영하면 정정 적용."""
    await _closed_appeal(session, "2026-08", TODAY - timedelta(days=20))
    cid = str(uuid.uuid4())
    await _submit(session, cid, 300_000, care_month="2026-08")
    await _review(session, cid, "PARTIALLY_APPROVED", 200_000, 100_000)
    ev = await _confirm(session, await _review(session, cid, "PARTIALLY_APPROVED", 250_000, 50_000))
    assert ev.status == "APPLIED", ev.error
    sub = await _sub(session, cid)
    assert float(sub.decided_amt) == 250_000 and sub.decided_rev == 2


async def test_correction_collision_message_points_to_resolve(session):
    """정정 충돌 DEAD도 실제 출구(처리 완료)를 가리킨다."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000, revision=1)
    ev = await _push(session, "claim.reviewed", f"claim:review:{cid}:x", {
        "claimId": cid, "status": "PARTIALLY_APPROVED", "approvedAmount": 300_000,
        "reducedAmount": 200_000, "revision": 1})
    assert ev.status == "DEAD" and "불일치" in ev.error and _HINT in ev.error


# ── 2. DEAD 종결(resolve) ─────────────────────────────────────────────────────

async def test_resolve_dead_event_records_and_audits(client, session):
    from erp.audit.models import AuditLog
    admin = await auth_as(session, user_id=301, role="ADMIN")
    ev = HisInboundEvent(domain="finance", event_type="claim.reviewed", source_ref="r3:dead:1",
                         payload={}, status="DEAD", error="ERP-2003: 가드")
    ok_ev = HisInboundEvent(domain="finance", event_type="claim.reviewed", source_ref="r3:failed:1",
                            payload={}, status="FAILED", error="일시")
    session.add_all([ev, ok_ev])
    await session.flush()
    eid, fid = ev.id, ok_ev.id
    url = f"/api/v1/integrations/his/inbound/{eid}/resolve"
    r = await client.post(url, headers=admin, json={"reason": "   "})
    assert r.status_code == 422, r.text
    r = await client.post(url, headers=admin, json={})
    assert r.status_code == 422, r.text
    r = await client.post(f"/api/v1/integrations/his/inbound/{fid}/resolve", headers=admin,
                          json={"reason": "x"})
    assert r.status_code == 422 and "DEAD" in r.text
    staff = await auth_as(session, user_id=302, role="STAFF")
    r = await client.post(url, headers=staff, json={"reason": "x"})
    assert r.status_code == 403, r.text
    r = await client.post(url, headers=admin, json={"reason": "심사팀 대조 — 이의 인정분 반영 확인"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "RESOLVED"
    row = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.id == eid))
    await session.refresh(row)
    assert row.status == "RESOLVED" and row.error == "ERP-2003: 가드"
    assert row.resolved_by == 301 and row.resolved_at is not None
    assert row.resolve_reason.startswith("심사팀")
    a = await session.scalar(select(AuditLog).where(
        AuditLog.entity == "integration.his_inbound_events", AuditLog.target_id == str(eid)))
    assert a is not None and a.action == "RESOLVE" and a.after["status"] == "RESOLVED"
    r = await client.post(url, headers=admin, json={"reason": "again"})     # 재종결 불가
    assert r.status_code == 422
    r = await client.get("/api/v1/integrations/his/inbound?status=RESOLVED", headers=admin)
    assert r.status_code == 200 and any(i["id"] == eid and i["resolve_reason"]
                                        for i in r.json()["items"])
    r = await client.get("/api/v1/integrations/his/inbound?status=BOGUS", headers=admin)
    assert r.status_code == 422
    # 재처리 대상 아님(RESOLVED)
    r = await client.post(f"/api/v1/integrations/his/inbound/{eid}/requeue", headers=admin)
    assert r.status_code == 422


def test_admin_menu_has_inbound_page():
    from erp.common.menu_service import MENU_CATALOG, MENU_READ_SCOPES
    items = [(h, roles) for _, grp in MENU_CATALOG for h, _, roles in grp]
    assert ("/admin/inbound", frozenset({"ADMIN"})) in items
    assert MENU_READ_SCOPES["/admin/inbound"] == ("admin.staging", "read")


# ── 3. 낡은 재생(수신 순서) ───────────────────────────────────────────────────

async def test_stale_rereview_replay_does_not_override_newer_decision(session):
    """LOW-MED — 부분(10만 삭감) → 전액 승인 순으로 반영된 뒤 앞선 이벤트가 재큐되면, 종전엔 v3 정정으로
    추론해 10만 삭감을 되살렸다(최신 결정 덮어씀). 이제 DEAD(출구 안내)·GL·확정액 불변."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    old = await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    new = await _confirm(session, await _review(session, cid, "APPROVED", 500_000, 0))
    assert new.status == "APPLIED", new.error
    old.status = "NEW"                                       # 운영자 재큐(같은 인박스 id)
    await session.flush()
    await consumer.consume(session)
    await session.flush()
    await session.refresh(old)
    assert old.status == "DEAD" and _HINT in old.error and "늦게 수신" in old.error
    sub = await _sub(session, cid)
    assert float(sub.decided_amt) == 500_000 and sub.decided_rev == 2
    n = await session.scalar(select(func.count(Voucher.id)).where(
        Voucher.source_module == "his.claim", Voucher.source_ref.like("claim:review:%:v3")))
    assert n == 0


async def test_stale_replay_same_amount_is_noop(session):
    """낡은 재생이 현재 반영 금액과 같으면 조용히 멱등 — 확정액·버전 불변."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    first = await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    await _confirm(session, await _review(session, cid, "APPROVED", 500_000, 0))
    await _push(session, "claim.reviewed", f"claim:review:{cid}:again", {
        "claimId": cid, "status": "PARTIALLY_APPROVED", "approvedAmount": 400_000,
        "reducedAmount": 100_000, "reviewedAt": "2026-10-01"})
    sub = await _sub(session, cid)
    assert sub.decided_rev == 3
    first.status = "NEW"
    await session.flush()
    out = await consumer.consume(session)
    await session.flush()
    await session.refresh(first)
    assert first.status == "APPLIED", (out, first.error)
    sub = await _sub(session, cid)
    assert sub.decided_rev == 3 and sub.decided_on == date(2026, 10, 1)


# ── 4. 청구 잠금 비차단(PG) ──────────────────────────────────────────────────

pg_only = pytest.mark.skipif(not _PG_URL, reason="advisory lock — TEST_DATABASE_URL(PG) 레인 전용")


def test_try_lock_is_noop_on_sqlite_path():
    assert issubclass(ic.ClaimLockBusy, Exception)
    assert not issubclass(ic.ClaimLockBusy, ic.ValidationError)    # DEAD 분기로 새지 않음


@pg_only
async def test_claim_lock_contention_fails_retryable_not_blocking(session):
    """LOW — 다른 트랜잭션(다른 워커 배치)이 같은 청구 잠금을 쥐고 있으면 종전엔 배치 끝까지 대기(교착·정체).
    이제 즉시 FAILED·짧은 백오프·retry_count 무증가."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from erp.common.doc_no import _NS
    cid = f"R3-LOCK-{uuid.uuid4().hex[:8]}"
    key = _NS | zlib.crc32(ic._bounded_ref("claim:review", cid, limit=ic._BASE_REF_MAX).encode())
    eng = create_async_engine(_PG_URL)
    sm = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with sm() as holder, sm() as worker:
            await holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
            worker.add(HisInboundEvent(domain="finance", event_type="claim.reviewed",
                                       source_ref=f"claim:review:{cid}:A", status="NEW", payload={
                                           "claimId": cid, "status": "PARTIALLY_APPROVED",
                                           "approvedAmount": 1, "reducedAmount": 1}))
            await worker.flush()
            await asyncio.wait_for(consumer.consume(worker), timeout=10)
            ev = await worker.scalar(select(HisInboundEvent).where(
                HisInboundEvent.source_ref == f"claim:review:{cid}:A"))
            assert ev.status == "FAILED" and ev.retry_count == 0 and ev.next_retry_at is not None
            assert "재시도" in ev.error
            await worker.rollback()
            await holder.rollback()
    finally:
        await eng.dispose()


# ── 4회차 검토 ────────────────────────────────────────────────────────────────

async def test_same_amount_resend_does_not_move_appeal_guard_window(session):
    """LOW-MED — 같은 삭감액 재수신(멱등)이 decided_on을 앞당겨, 그 사이 종결된 이의가 기준보다 앞서 보여
    가드를 빠져나갔다 → 이중 복원. 기준은 GL 전기 일자. (5회차: 복원 방향은 이의 유무와 무관하게 확인 보류 —
    결정일·확정액이 멱등 재수신으로 움직이지 않는지는 계속 확인)"""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000,
                  reviewedAt=(TODAY - timedelta(days=30)).isoformat())
    await _closed_appeal(session, "2026-02", TODAY - timedelta(days=20))
    same = await _review(session, cid, "PARTIAL", 400_000, 100_000,       # 상태 표기만 다른 재수신
                         reviewedAt=(TODAY - timedelta(days=10)).isoformat())
    assert same.status == "APPLIED", same.error
    ev = await _review(session, cid, "APPROVED", 500_000, 0)
    assert ev.status == "DEAD" and _HINT in ev.error
    assert float((await _sub(session, cid)).decided_amt) == 400_000


async def test_conflict_message_does_not_instruct_blocked_manual_reversal(session):
    """MED — 충돌 DEAD 메시지가 '역분개한 뒤'를 지시했지만 심사 전표 수동 역분개는 막혀 있다."""
    from erp.errors import ValidationError
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    with pytest.raises(ValidationError) as ei:
        await ic._assert_no_conflicting_voucher(session, source_module="his.claim",
                                                source_ref=f"claim:review:{cid}", expected_debit=50_000)
    assert "역분개" not in str(ei.value) and _HINT in str(ei.value)


async def test_lis_status_reports_resolved_as_rejected(session, monkeypatch):
    """LOW — 관리자가 종결(RESOLVED)한 LIS 이벤트를 폴링이 PENDING으로 보고해 LIS가 영원히 대기했다."""
    import hashlib
    import hmac
    import time

    from erp.config import get_settings
    from erp.integrations.lis_webhook_router import lis_billing_status
    monkeypatch.setattr(get_settings(), "lis_billing_secret", "lis-secret")
    session.add(HisInboundEvent(domain="lis", event_type="lis.billing", source_ref="lis:L-R4:1",
                                payload={}, status="RESOLVED", error="items 없음",
                                resolve_reason="LIS 측 취소 확인"))
    await session.flush()
    ts = str(int(time.time()))
    sig = hmac.new(b"lis-secret", f"{ts}.L-R4".encode(), hashlib.sha256).hexdigest()
    out = await lis_billing_status("L-R4", x_integration_signature=sig, x_integration_ts=ts,
                                   session=session)
    assert out["status"] == "REJECTED" and "LIS 측 취소 확인" in out["error"]


async def test_increasing_cut_is_never_blocked_by_appeal_guard(session):
    """삭감을 늘리는 재심사는 복원 방향이 아니라 이의 이중 복원 위험이 없다."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    await _closed_appeal(session, "2026-02", TODAY)
    ev = await _review(session, cid, "PARTIALLY_APPROVED", 300_000, 200_000)
    assert ev.status == "APPLIED", ev.error


async def test_decided_on_uses_kst_date(session):
    """HIS reviewedAt은 UTC(Z) — KST 01:30 결정이 전날로 잡히던 것."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000,
                  reviewedAt="2026-09-30T16:30:00.000Z")              # KST 2026-10-01 01:30
    assert (await _sub(session, cid)).decided_on == date(2026, 10, 1)
