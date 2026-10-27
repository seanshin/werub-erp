"""2026-10-09 전체 흐름 검토 B — 심사 정정·이의신청·인식 AR 정합."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from erp.claims.models import ClaimSubmission
from erp.claims.service import AppealService
from erp.errors import ValidationError
from erp.finance.models import VoucherLine
from erp.integrations.inbound_consumer import InboundConsumer
from tests.test_claims import TODAY, _adjustment
from tests.test_inbound_consumer import _ev

consumer = InboundConsumer()


async def _gl(session, acct):
    d = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.account_code == acct, VoucherLine.side == "DEBIT"))
    c = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.account_code == acct, VoucherLine.side == "CREDIT"))
    return float(d) - float(c)


async def _submit(session, cid="C-1", amt=1_000_000):
    session.add(_ev("finance", "claim.submitted", f"S-{cid}", {
        "claimId": cid, "claimNumber": cid, "claimedAmount": amt, "careMonth": "2026-09"}))
    await session.flush()
    await consumer.consume(session)


async def test_revision_with_zero_cut_reverses_prior_loss(session):
    """v1 삭감 10만 → v2 전액 승인: 5910 순액 0·1140 복원(종전 v1 삭감 잔존)."""
    await _submit(session)
    session.add(_ev("finance", "claim.reviewed", "R1", {
        "claimId": "C-1", "claimedAmount": 1_000_000, "approvedAmount": 900_000,
        "reducedAmount": 100_000, "status": "PARTIAL"}))
    await session.flush()
    await consumer.consume(session)
    assert await _gl(session, "5910") == 100_000
    session.add(_ev("finance", "claim.reviewed", "R2", {
        "claimId": "C-1", "claimedAmount": 1_000_000, "approvedAmount": 1_000_000,
        "reducedAmount": 0, "status": "APPROVED", "revision": 2}))
    await session.flush()
    await consumer.consume(session)
    assert await _gl(session, "5910") == 0
    assert await _gl(session, "1140") == 1_000_000
    # v3 삭감 5만 — 이미 역분개된 원본을 다시 역분개하지 않는다(체인)
    session.add(_ev("finance", "claim.reviewed", "R3", {
        "claimId": "C-1", "claimedAmount": 1_000_000, "approvedAmount": 950_000,
        "reducedAmount": 50_000, "status": "PARTIAL", "revision": 3}))
    await session.flush()
    await consumer.consume(session)
    assert await _gl(session, "5910") == 50_000


async def test_revision_updates_decided_amt(session):
    """정정 승인액이 확정액에 반영(종전 최초 리뷰만 → 입금이 2141로 오분리)."""
    await _submit(session, "C-2")
    for ref, approved, rev in (("R2-1", 900_000, 1), ("R2-2", 950_000, 2)):
        session.add(_ev("finance", "claim.reviewed", ref, {
            "claimId": "C-2", "claimedAmount": 1_000_000, "approvedAmount": approved,
            "reducedAmount": 1_000_000 - approved, "revision": rev}))
        await session.flush()
        await consumer.consume(session)
    sub = await session.scalar(select(ClaimSubmission).where(ClaimSubmission.claim_id == "C-2"))
    assert float(sub.decided_amt) == 950_000 and sub.decided_rev == 2


async def test_reduced_only_payload_sets_decided_amt(session):
    """approvedAmount 없이 reducedAmount만 — 확정액 = 청구−삭감(종전 None → 인식 AR 0)."""
    await _submit(session, "C-3")
    session.add(_ev("finance", "claim.reviewed", "R3-1", {
        "claimId": "C-3", "claimedAmount": 1_000_000, "reducedAmount": 100_000}))
    await session.flush()
    await consumer.consume(session)
    sub = await session.scalar(select(ClaimSubmission).where(ClaimSubmission.claim_id == "C-3"))
    assert float(sub.decided_amt) == 900_000


async def test_appeal_duplicate_blocked_and_range_checked(session):
    svc = AppealService()
    adj = await _adjustment(session, amt=100_000)
    arrived = TODAY - timedelta(days=5)
    a1 = await svc.open(session, adjustment_id=adj.id, appeal_type="REVIEW_REQUEST",
                        notice_arrived_on=arrived, actor=1)
    with pytest.raises(ValidationError, match="진행 중"):
        await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                       notice_arrived_on=arrived, actor=1)
    with pytest.raises(ValidationError, match="최종 손실액"):
        await svc.transition(session, appeal_id=a1.id, status="ACCEPTED", actor=1,
                             final_loss_amt=-50_000)
    await svc.transition(session, appeal_id=a1.id, status="REJECTED", actor=1)
    # 기각 후 상위 이의는 허용
    await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                   notice_arrived_on=arrived, actor=1)


async def test_recognized_ar_includes_appeal_restoration(session):
    """인정으로 복원된 1140이 인식 AR에 포함 — 재지급이 2141로 가지 않게."""
    from erp.claims.service import ClaimPaymentService
    adj = await _adjustment(session, amt=100_000)          # care_month 2026-04
    session.add(ClaimSubmission(claim_id="CX", claimed_amt=1_000_000, care_month="2026-04",
                                decided_on=TODAY, decided_amt=900_000))
    await session.flush()
    svc = AppealService()
    a = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                       notice_arrived_on=TODAY - timedelta(days=5), actor=1)
    await svc.transition(session, appeal_id=a.id, status="ACCEPTED", actor=1)
    assert await ClaimPaymentService()._recognized_ar(session, "2026-04") == 1_000_000
