"""벤더 관리 — 상태 생애주기·평가 스코어카드 (구매 거버넌스)."""
import pytest

from erp.errors import ValidationError
from erp.scm.models import Supplier
from erp.scm.vendor_service import VendorService

svc = VendorService()


async def _sup(session):
    s = Supplier(name="대한약품", business_no="1018212345")
    session.add(s)
    await session.flush()
    return s


async def test_status_lifecycle_syncs_is_active(session):
    s = await _sup(session)
    await svc.set_status(session, supplier_id=s.id, status="SUSPENDED")
    assert s.status == "SUSPENDED" and s.is_active is False
    await svc.set_status(session, supplier_id=s.id, status="APPROVED")
    assert s.is_active is True
    with pytest.raises(ValidationError):
        await svc.set_status(session, supplier_id=s.id, status="BOGUS")


async def test_terminated_vendor_cannot_be_revived(session):
    """심층 재검토 — 해지(TERMINATED) 벤더는 상태 전이 불가(부활은 신규 등록·블랙리스트 우회 차단)."""
    s = await _sup(session)
    await svc.set_status(session, supplier_id=s.id, status="TERMINATED")
    assert s.status == "TERMINATED" and s.is_active is False
    with pytest.raises(ValidationError, match="상태 전이"):
        await svc.set_status(session, supplier_id=s.id, status="APPROVED")   # 부활 차단


async def test_evaluate_weighted_total_and_eval_score(session):
    s = await _sup(session)
    ev = await svc.evaluate(session, supplier_id=s.id, period="2026-06",
                            delivery=90, quality=80, price=70)
    # 90*0.4 + 80*0.4 + 70*0.2 = 36 + 32 + 14 = 82.0
    assert float(ev.total) == 82.0 and float(s.eval_score) == 82.0
    # 동일 기간 재평가 → 갱신(중복 없음)
    ev2 = await svc.evaluate(session, supplier_id=s.id, period="2026-06",
                             delivery=100, quality=100, price=100)
    assert float(ev2.total) == 100.0
    card = await svc.scorecard(session, supplier_id=s.id)
    assert len(card["evaluations"]) == 1 and card["evaluations"][0]["total"] == 100.0


async def test_evaluate_score_range_guard(session):
    s = await _sup(session)
    with pytest.raises(ValidationError, match="0~100"):
        await svc.evaluate(session, supplier_id=s.id, period="2026-06",
                           delivery=120, quality=80, price=70)
