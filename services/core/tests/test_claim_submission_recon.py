"""청구액 이중소스 정합 — 제출 캡처(claim.submitted)·진료월 대사·미동봉 표면화."""
from datetime import date

from sqlalchemy import select

from erp.claims.models import ClaimBatch, ClaimStatement, ClaimSubmission
from erp.claims.service import ClaimSubmissionService
from erp.integrations.inbound_consumer import InboundConsumer
from erp.integrations.models import HisInboundEvent

svc = ClaimSubmissionService()
consumer = InboundConsumer()


def _ev(ref, payload):
    return HisInboundEvent(domain="finance", event_type="claim.submitted", source_ref=ref,
                           payload=payload, status="NEW")


async def _stmt(session, care_month="2026-06", claimed=100_000, serial="001"):
    b = await session.scalar(select(ClaimBatch).where(ClaimBatch.care_month == care_month))
    if b is None:
        b = ClaimBatch(receipt_no=f"R-{care_month}", care_month=care_month,
                       notice_arrived_on=date(2026, 7, 1))
        session.add(b)
        await session.flush()
    session.add(ClaimStatement(batch_id=b.id, stmt_serial=serial, claimed_amt=claimed))
    await session.flush()


async def test_claim_submitted_captures_submission(session):
    """claim.submitted → AR 전기 + 제출 청구 캡처(care_month 정규화·claim_id 멱등)."""
    session.add(_ev("EVT-S1", {"claimId": "C-1", "claimNumber": "CLM-1",
                               "claimedAmount": 120000, "careMonth": "202606"}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    s = await session.scalar(select(ClaimSubmission).where(ClaimSubmission.claim_id == "C-1"))
    assert s and s.care_month == "2026-06" and float(s.claimed_amt) == 120000


async def test_submission_capture_idempotent(session):
    """동일 claim_id 재수신 → 제출 캡처 증식 없음(멱등)."""
    for ref in ("EVT-D1", "EVT-D2"):   # 다른 인박스 이벤트지만 같은 claimId
        session.add(_ev(ref, {"claimId": "C-DUP", "claimedAmount": 50000, "careMonth": "202606"}))
    await session.flush()
    await consumer.consume(session)
    subs = (await session.scalars(
        select(ClaimSubmission).where(ClaimSubmission.claim_id == "C-DUP"))).all()
    assert len(subs) == 1


async def test_reconciliation_matched_and_variance(session):
    """진료월 대사 — 제출액=심사대상 청구액이면 matched, 아니면 variance 탐지."""
    # 제출 2건 합 300,000
    session.add_all([
        _ev("EVT-R1", {"claimId": "R-1", "claimedAmount": 100000, "careMonth": "202606"}),
        _ev("EVT-R2", {"claimId": "R-2", "claimedAmount": 200000, "careMonth": "202606"})])
    await session.flush()
    await consumer.consume(session)
    # 심사대상(MEDRLT) 청구 2건 합 300,000 → 일치
    await _stmt(session, claimed=100_000, serial="001")
    await _stmt(session, claimed=200_000, serial="002")
    r = await svc.reconciliation(session, care_month="2026-06")
    assert r["submitted"] == 300_000 and r["reviewed_claimed"] == 300_000
    assert r["variance"] == 0 and r["matched"] is True
    # 심사대상에 50,000 추가 → 제출<심사(누락 의심) variance -50,000
    await _stmt(session, claimed=50_000, serial="003")
    r2 = await svc.reconciliation(session, care_month="2026-06")
    assert r2["variance"] == -50_000 and r2["matched"] is False


async def test_unlinked_submissions_surfaced(session):
    """진료월 미동봉(care_month NULL) 제출은 미대사로 표면화."""
    session.add(_ev("EVT-U1", {"claimId": "U-1", "claimedAmount": 70000}))  # careMonth 없음
    await session.flush()
    await consumer.consume(session)
    out = await svc.unlinked_submissions(session)
    assert out["count"] == 1 and out["amount"] == 70000
    assert out["items"][0]["claim_id"] == "U-1"


async def test_claim_reviewed_marks_submission_decided(session):
    """claim.reviewed → 제출 청구 decided_on 마킹(변동대가 풀 정렬·실적과 동일 신호)."""
    session.add(_ev("EVT-SUB", {"claimId": "MK-1", "claimedAmount": 100000, "careMonth": "202606"}))
    await session.flush()
    await consumer.consume(session)
    assert (await session.scalar(select(ClaimSubmission).where(
        ClaimSubmission.claim_id == "MK-1"))).decided_on is None
    # 심사 결과 수신 → 결정 마킹
    rev = HisInboundEvent(domain="finance", event_type="claim.reviewed", source_ref="EVT-REV",
                          payload={"claimId": "MK-1", "status": "PARTIALLY_APPROVED",
                                   "approvedAmount": 90000, "reducedAmount": 10000,
                                   "reviewedAt": "2026-07-05"}, status="NEW")
    session.add(rev)
    await session.flush()
    await consumer.consume(session)
    s = await session.scalar(select(ClaimSubmission).where(ClaimSubmission.claim_id == "MK-1"))
    assert s.decided_on == date(2026, 7, 5) and float(s.decided_amt) == 90000


async def test_router_submission_reconciliation(session):
    from erp.claims.router import claim_submission_reconciliation
    from erp.common.router import CurrentUser
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    session.add(_ev("EVT-RT1", {"claimId": "RT-1", "claimedAmount": 80000, "careMonth": "202606"}))
    await session.flush()
    await consumer.consume(session)
    await _stmt(session, claimed=80_000)
    recon = await claim_submission_reconciliation(care_month="2026-06", user=admin, session=session)
    assert recon["matched"] is True and recon["submitted"] == 80_000
