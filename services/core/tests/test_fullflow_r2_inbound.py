"""2026-10-09 전체 흐름 검토 2차 — 연동·청구 보완 회귀."""
from datetime import timedelta

from sqlalchemy import func, select

from erp.claims.service import AppealService
from erp.finance.models import Voucher, VoucherLine
from erp.integrations.inbound_consumer import InboundConsumer
from tests.test_claims import TODAY, _adjustment
from tests.test_inbound_consumer import _ev

consumer = InboundConsumer()


async def test_settled_excludes_voucher_and_deposit(session):
    """검진권·예치금은 현금으로 잡지 않는다(이중·과대 계상)."""
    session.add(_ev("finance", "billing.settled", "R2-S1", {"date": "2026-10-05", "summary": {"data": {
        "totalRevenue": 600_000,
        "byPaymentMethod": {"cash": 100_000, "card": 200_000, "voucher": 150_000, "deposit": 150_000}}}}))
    await session.flush()
    await consumer.consume(session)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "settle:2026-10-05"))
    cash = await session.scalar(select(func.sum(VoucherLine.amount)).where(
        VoucherLine.voucher_id == v.id, VoucherLine.account_code == "1110"))
    assert float(cash) == 100_000


async def test_two_refunds_same_revision_both_posted(session):
    """HIS revision 경합으로 같은 revision이어도 이벤트 ref가 다르면 둘 다 전표."""
    for i in range(2):
        session.add(_ev("finance", "billing.adjusted", f"billing:refund:B-SR:{i}", {
            "billingId": "B-SR", "type": "REFUND", "amount": 10_000, "revision": 2,
            "at": "2026-10-05T03:00:00.000Z"}))
    await session.flush()
    await consumer.consume(session)
    n = await session.scalar(select(func.count()).select_from(Voucher).where(
        Voucher.source_ref.like("adjust:B-SR:REFUND:%")))
    assert n == 2


async def test_sequential_appeals_do_not_double_restore(session):
    """부분 기각(손실 70) 뒤 상위 이의 인정 → 누적 복원 100(종전 30+100=130)."""
    from erp.claims.service import ClaimPaymentService
    from erp.claims.models import ClaimSubmission
    adj = await _adjustment(session, amt=100)
    session.add(ClaimSubmission(claim_id="SEQ", claimed_amt=1000, care_month="2026-04",
                                decided_on=TODAY, decided_amt=900))
    await session.flush()
    svc = AppealService()
    a1 = await svc.open(session, adjustment_id=adj.id, appeal_type="REVIEW_REQUEST",
                        notice_arrived_on=TODAY - timedelta(days=5), actor=1)
    await svc.transition(session, appeal_id=a1.id, status="REJECTED", actor=1, final_loss_amt=70)
    a2 = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                        notice_arrived_on=TODAY - timedelta(days=5), actor=1)
    await svc.transition(session, appeal_id=a2.id, status="ACCEPTED", actor=1)
    restored = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.account_code == "1140", VoucherLine.side == "DEBIT",
        VoucherLine.voucher_id.in_(select(Voucher.id).where(Voucher.source_ref.like("appeal-adj-%")))))
    assert float(restored) == 100
    assert await ClaimPaymentService()._recognized_ar(session, "2026-04") == 1000


async def test_retired_status_does_not_dispose(session):
    """RETIRED·불용은 되돌릴 수 있는 상태 — 처분 전표 없이 상태만."""
    from erp.finance.models import FixedAsset
    session.add(_ev("assets", "equipment.acquired", "RT-A", {
        "assetNumber": "EQ-RT", "name": "장비", "purchasePrice": 1_000_000, "purchaseDate": "2025-01-01"}))
    await session.flush()
    await consumer.consume(session)
    session.add(_ev("assets", "equipment.status_changed", "RT-S", {"assetNumber": "EQ-RT", "status": "RETIRED"}))
    await session.flush()
    await consumer.consume(session)
    a = await session.scalar(select(FixedAsset).where(FixedAsset.asset_no == "EQ-RT"))
    assert a.status != "DISPOSED"


async def test_requeue_rejects_applied(session, client):
    from erp.integrations.models import HisInboundEvent
    from tests._http import auth_as
    admin = await auth_as(session, user_id=1, role="ADMIN")
    ev = HisInboundEvent(domain="finance", event_type="x", source_ref="RQ-1", payload={}, status="APPLIED")
    session.add(ev)
    await session.flush()
    r = await client.post(f"/api/v1/integrations/his/inbound/{ev.id}/requeue", headers=admin)
    assert r.status_code == 422, r.text


async def test_three_step_appeals_restore_consistent(session):
    """3차 검토 B — 기각(손실 60) → 기각(기본) → 인정: GL 복원 100 = 인식 AR 복원 100."""
    from erp.claims.models import ClaimSubmission
    from erp.claims.service import ClaimPaymentService
    adj = await _adjustment(session, amt=100)
    session.add(ClaimSubmission(claim_id="S3", claimed_amt=1000, care_month="2026-04",
                                decided_on=TODAY, decided_amt=900))
    await session.flush()
    svc = AppealService()
    for kind, status, fl in (("REVIEW_REQUEST", "REJECTED", 60), ("OBJECTION", "REJECTED", None),
                             ("OBJECTION", "ACCEPTED", None)):
        a = await svc.open(session, adjustment_id=adj.id, appeal_type=kind,
                           notice_arrived_on=TODAY - timedelta(days=5), actor=1)
        await svc.transition(session, appeal_id=a.id, status=status, actor=1, final_loss_amt=fl)
    gl = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.account_code == "1140", VoucherLine.side == "DEBIT",
        VoucherLine.voucher_id.in_(select(Voucher.id).where(Voucher.source_ref.like("appeal-adj-%")))))
    assert float(gl) == 100
    assert await ClaimPaymentService()._recognized_ar(session, "2026-04") == 1000
