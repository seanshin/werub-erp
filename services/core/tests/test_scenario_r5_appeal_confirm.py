"""2026-10-09 시나리오 5회차 — revision 없는 HIS 재심사의 삭감 감소(복원) 방향은 추측하지 않고 운영자 확인.

HIS claim.reviewed엔 revision이 없고, ERP 이의신청 인정 복원(claims `appeal-adj-{id}` DR1140/CR5910)과 HIS 청구
사이에 연결이 없다(#621). 날짜 가드(오탐)·id+정확 금액 가드(복수 조정·부분 인정·연속 이의 이중 복원)를 폐기하고:
- 삭감 감소 + 표식 없음 → 즉시 DEAD(출구: 관리자 > 연동 인박스 '확인 후 반영' / '처리 완료(사유)').
- 삭감 증가·같은 금액·명시 revision → 종전대로 자동.
- POST /integrations/his/inbound/{id}/apply-confirmed(사유 필수·DEAD claim.reviewed만·감사) → 표식 동봉 재처리.
"""
import uuid

from sqlalchemy import func, select, update

from erp.claims.models import ClaimSubmission
from erp.finance.models import Voucher, VoucherLine
from erp.integrations.inbound_consumer import InboundConsumer
from erp.integrations.models import HisInboundEvent
from tests._http import auth_as

consumer = InboundConsumer()
_HOLD = "확인 후 반영"
_MARK = "_erpConfirmedRestore"


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
    await session.flush()
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == ref))
    await session.refresh(ev)
    return ev


async def _submit(session, cid, amt):
    ev = await _push(session, "claim.submitted", f"claim:submit:{cid}",
                     {"claimId": cid, "claimNumber": f"CLM-{cid[:8]}", "claimedAmount": amt})
    assert ev.status == "APPLIED", ev.error


async def _review(session, cid, status, approved, reduced, **extra):
    ref = f"claim:review:{cid}:{status}:{approved}:{reduced}"
    if "revision" in extra:
        ref += f":r{extra['revision']}"
    return await _push(session, "claim.reviewed", ref, {
        "claimId": cid, "claimNumber": f"CLM-{cid[:8]}", "status": status,
        "approvedAmount": approved, "reducedAmount": reduced, **extra})


async def _sub(session, cid):
    s = await session.scalar(select(ClaimSubmission).where(ClaimSubmission.claim_id == cid))
    await session.refresh(s)
    return s


async def _consume_again(session, ev):
    await consumer.consume(session)
    await session.flush()
    await session.refresh(ev)
    return ev


# ── 소비자 판정 ───────────────────────────────────────────────────────────────

async def test_restoration_held_even_without_any_appeal(session):
    """이의가 하나도 없어도 삭감 감소(10만→0)는 자동 반영하지 않는다 — 추측 대신 운영자 확인(fail-closed)."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    ev = await _review(session, cid, "APPROVED", 500_000, 0)
    assert ev.status == "DEAD" and _HOLD in ev.error and "처리 완료(사유)" in ev.error
    assert "이의신청" in ev.error
    assert await _gl(session, "5910") == 100_000 and await _gl(session, "1140") == 400_000
    sub = await _sub(session, cid)
    assert float(sub.decided_amt) == 400_000 and sub.decided_rev == 1


async def test_cut_increase_still_auto_applies_after_held_restoration(session):
    """삭감 증가는 이의 복원과 겹치지 않아 자동 — 보류된 감소 건(v2 아님)을 건너 v2로 반영."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    held = await _review(session, cid, "PARTIALLY_APPROVED", 450_000, 50_000)
    assert held.status == "DEAD" and _HOLD in held.error
    ev = await _review(session, cid, "PARTIALLY_APPROVED", 300_000, 200_000)
    assert ev.status == "APPLIED", ev.error
    assert await _gl(session, "5910") == 200_000 and await _gl(session, "1140") == 300_000
    sub = await _sub(session, cid)
    assert float(sub.decided_amt) == 300_000 and sub.decided_rev == 2


async def test_identical_amount_is_noop_while_restoration_held(session):
    """같은 금액 재수신은 멱등 no-op — 보류된 감소 건이 GL·버전을 움직이지 않았으므로 그대로."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    await _review(session, cid, "APPROVED", 500_000, 0)                        # 보류
    ev = await _push(session, "claim.reviewed", f"redeliver:{cid}", {
        "claimId": cid, "status": "PARTIALLY_APPROVED", "approvedAmount": 400_000,
        "reducedAmount": 100_000})
    assert ev.status == "APPLIED", ev.error
    assert await _gl(session, "5910") == 100_000
    assert (await _sub(session, cid)).decided_rev == 1


async def test_explicit_revision_restoration_auto_applies_but_inferred_is_held(session):
    """HIS가 revision을 실으면 HIS가 정정을 명시한 것 — 자동. revision 없는 감소는 보류."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000, revision=1)
    ev = await _review(session, cid, "PARTIALLY_APPROVED", 450_000, 50_000, revision=2)
    assert ev.status == "APPLIED", ev.error
    assert await _gl(session, "5910") == 50_000
    inferred = await _review(session, cid, "APPROVED", 500_000, 0)
    assert inferred.status == "DEAD" and _HOLD in inferred.error
    assert await _gl(session, "5910") == 50_000


async def test_confirmed_marker_does_not_bypass_stale_replay_guard(session):
    """확인 표식이 있어도 낡은 재생 가드는 그대로 — 보류 사이에 늦게 수신된 결정이 반영됐으면 덮지 않는다."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    held = await _review(session, cid, "APPROVED", 500_000, 0)
    newer = await _review(session, cid, "PARTIALLY_APPROVED", 300_000, 200_000)  # 증가 — 자동
    assert held.status == "DEAD" and newer.status == "APPLIED", newer.error
    held.payload = {**held.payload, _MARK: {"by": 1, "reason": "대조"}}
    held.status, held.error = "NEW", None
    await session.flush()
    held = await _consume_again(session, held)
    assert held.status == "DEAD" and "늦게 수신" in held.error
    assert await _gl(session, "5910") == 200_000


# ── 확인 후 반영 EP ───────────────────────────────────────────────────────────

async def test_apply_confirmed_endpoint_authz_validation_audit_and_apply(client, session):
    from erp.audit.models import AuditLog
    admin = await auth_as(session, user_id=501, role="ADMIN")
    staff = await auth_as(session, user_id=502, role="STAFF")
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    held = await _review(session, cid, "APPROVED", 500_000, 0)
    assert held.status == "DEAD" and _HOLD in held.error
    other = HisInboundEvent(domain="finance", event_type="billing.recorded", source_ref="r5:other",
                            payload={}, status="DEAD", error="x")
    failed = HisInboundEvent(domain="finance", event_type="claim.reviewed", source_ref="r5:failed",
                             payload={}, status="FAILED", error="일시")
    session.add_all([other, failed])
    await session.flush()
    base = "/api/v1/integrations/his/inbound"
    url = f"{base}/{held.id}/apply-confirmed"
    r = await client.post(url, headers=staff, json={"reason": "x"})
    assert r.status_code == 403, r.text
    r = await client.post(url, headers=admin, json={"reason": "   "})
    assert r.status_code == 422, r.text
    r = await client.post(url, headers=admin, json={})
    assert r.status_code == 422, r.text
    r = await client.post(url, headers=admin, json={"reason": "x" * 301})
    assert r.status_code == 422, r.text
    r = await client.post(f"{base}/{other.id}/apply-confirmed", headers=admin, json={"reason": "x"})
    assert r.status_code == 422 and "claim.reviewed" in r.text
    r = await client.post(f"{base}/{failed.id}/apply-confirmed", headers=admin, json={"reason": "x"})
    assert r.status_code == 422 and "DEAD" in r.text
    r = await client.post(url, headers=admin, json={"reason": "  이의 결과 대조 — 미반영 확인  "})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "NEW"
    await session.refresh(held)
    assert held.status == "NEW" and held.retry_count == 0 and held.error is None
    mark = held.payload[_MARK]
    assert mark["by"] == 501 and mark["reason"] == "이의 결과 대조 — 미반영 확인" and mark["at"]
    assert held.payload["claimId"] == cid                                   # 원 payload 보존
    a = await session.scalar(select(AuditLog).where(
        AuditLog.entity == "integration.his_inbound_events", AuditLog.target_id == str(held.id)))
    assert a is not None and a.action == "CONFIRM_APPLY" and a.after["status"] == "NEW"
    assert a.before["status"] == "DEAD"
    held = await _consume_again(session, held)
    assert held.status == "APPLIED", held.error
    assert await _gl(session, "5910") == 0 and await _gl(session, "1140") == 500_000
    sub = await _sub(session, cid)
    assert float(sub.decided_amt) == 500_000 and sub.decided_rev == 2
    r = await client.post(url, headers=admin, json={"reason": "again"})     # APPLIED — 재확인 불가
    assert r.status_code == 422


async def test_payload_replacement_drops_marker_and_is_held_again(client, session):
    """확인 표식은 그 인박스 행 payload에만 있다 — HIS 재발행으로 payload가 갈리면(갱신 경로) 다시 보류."""
    admin = await auth_as(session, user_id=503, role="ADMIN")
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 300_000, 200_000)
    held = await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    r = await client.post(f"/api/v1/integrations/his/inbound/{held.id}/apply-confirmed",
                          headers=admin, json={"reason": "대조 완료"})
    assert r.status_code == 200, r.text
    await session.refresh(held)
    assert _MARK in held.payload
    # 웹훅 갱신성 경로와 같은 방식(Core UPDATE로 payload 통째 교체·NEW) — 금액이 더 줄어든 재발행
    new_payload = {"claimId": cid, "claimNumber": f"CLM-{cid[:8]}", "status": "APPROVED",
                   "approvedAmount": 500_000, "reducedAmount": 0}
    await session.execute(update(HisInboundEvent).where(HisInboundEvent.id == held.id).values(
        payload=new_payload, status="NEW", retry_count=0, next_retry_at=None, error=None))
    await session.flush()
    held = await _consume_again(session, held)
    assert held.status == "DEAD" and _HOLD in held.error
    assert await _gl(session, "5910") == 200_000                            # 반영 안 됨
    n = await session.scalar(select(func.count(Voucher.id)).where(
        Voucher.source_module == "his.claim", Voucher.source_ref.like(f"claim:review:{cid}%")))
    assert n == 1
