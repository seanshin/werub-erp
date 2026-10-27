"""구매 거버넌스 (FR-14/09) — 직무분리 승인·예산 약정 연계·3-way 입고검수·procure-to-pay."""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.errors import ValidationError
from erp.finance.models import Budget, Encumbrance, Payable, Voucher
from erp.scm.models import Item, Supplier
from erp.scm.po_service import PurchaseOrderService

svc = PurchaseOrderService()


async def _seed(session):
    sup = Supplier(name="대한약품", business_no="1018212345")
    item = Item(code="M001", name="수액세트", category="SUPPLY", is_narcotic=False,
                safety_stock_qty=10)
    session.add_all([sup, item])
    await session.flush()
    return sup, item


async def test_receive_creates_payable_and_voucher(session):
    sup, item = await _seed(session)
    po = await svc.request(session, supplier_id=sup.id, actor=1,
                           lines=[{"item_id": item.id, "qty": 100, "unit_price": 5000}])
    await svc.approve(session, po_id=po.id, actor=2)   # 직무분리 — 요청자≠승인자
    await svc.receive(session, po_id=po.id, actor=2, received_on=date(2026, 5, 1),
                      receipts=[{"item_id": item.id, "lot_no": "L1",
                                 "expiry_on": date(2027, 5, 1), "qty": 100}])
    assert po.status == "RECEIVED"
    pay = (await session.execute(
        select(Payable).where(Payable.source_ref == f"po-{po.id}-r1"))).scalar_one()
    assert float(pay.amount) == 500000.0 and pay.party == "대한약품"
    vc = await session.scalar(select(func.count()).select_from(Voucher)
                              .where(Voucher.source_ref == f"ap-bill-{pay.id}"))
    assert vc == 1


async def test_po_to_inactive_supplier_blocked(session):
    """심층 재검토 — 정지/해지 공급사 발주 차단(벤더 생애주기 게이트)."""
    sup, item = await _seed(session)
    sup.is_active = False
    sup.status = "SUSPENDED"
    await session.flush()
    with pytest.raises(ValidationError, match="발주 가능한 공급사"):
        await svc.request(session, supplier_id=sup.id, actor=1,
                          lines=[{"item_id": item.id, "qty": 10, "unit_price": 5000}])


async def test_receive_negative_qty_blocked(session):
    """심층 재검토 — 음수/0 입고 차단(재고 역감·음수 미지급·초과입고 우회 방지)."""
    sup, item = await _seed(session)
    po = await svc.request(session, supplier_id=sup.id, actor=1,
                           lines=[{"item_id": item.id, "qty": 100, "unit_price": 5000}])
    await svc.approve(session, po_id=po.id, actor=2)
    with pytest.raises(ValidationError, match="0보다 커야"):
        await svc.receive(session, po_id=po.id, actor=2, received_on=date(2026, 5, 1),
                          receipts=[{"item_id": item.id, "lot_no": "L1",
                                     "expiry_on": date(2027, 5, 1), "qty": -40}])


async def test_approve_requires_segregation(session):
    """직무분리 — 요청자 본인 승인 차단."""
    sup, item = await _seed(session)
    po = await svc.request(session, supplier_id=sup.id, actor=1,
                           lines=[{"item_id": item.id, "qty": 10, "unit_price": 5000}])
    with pytest.raises(ValidationError, match="직무분리"):
        await svc.approve(session, po_id=po.id, actor=1)


async def test_approve_encumbers_budget_and_release_on_receipt(session):
    """승인 시 예산 약정(OPEN) → 완납 시 해제(RELEASED)."""
    sup, item = await _seed(session)
    session.add(Budget(department_id=7, fiscal_year=date.today().year,
                       account_code="1150", amount=10_000_000))
    po = await svc.request(session, supplier_id=sup.id, actor=1, department_id=7,
                           lines=[{"item_id": item.id, "qty": 100, "unit_price": 5000}])
    await svc.approve(session, po_id=po.id, actor=2)
    enc = (await session.execute(select(Encumbrance).where(
        Encumbrance.source_ref == f"po-{po.id}"))).scalar_one()
    assert enc.status == "OPEN" and float(enc.amount) == 500000.0
    await svc.receive(session, po_id=po.id, actor=2, received_on=date(2026, 5, 1),
                      receipts=[{"item_id": item.id, "lot_no": "L1",
                                 "expiry_on": date(2027, 5, 1), "qty": 100}])
    await session.flush()
    assert enc.status == "RELEASED"


async def test_receive_over_order_blocked(session):
    """3-way — 발주수량 초과 입고 차단."""
    sup, item = await _seed(session)
    po = await svc.request(session, supplier_id=sup.id, actor=1,
                           lines=[{"item_id": item.id, "qty": 100, "unit_price": 5000}])
    await svc.approve(session, po_id=po.id, actor=2)
    with pytest.raises(ValidationError, match="초과"):
        await svc.receive(session, po_id=po.id, actor=2, received_on=date(2026, 5, 1),
                          receipts=[{"item_id": item.id, "lot_no": "L1",
                                     "expiry_on": date(2027, 5, 1), "qty": 120}])


async def test_partial_then_full_multi_receipt(session):
    """분할입고 — PARTIAL→RECEIVED, 입고분만 미지급(멱등키 r1·r2 분리)."""
    sup, item = await _seed(session)
    po = await svc.request(session, supplier_id=sup.id, actor=1,
                           lines=[{"item_id": item.id, "qty": 100, "unit_price": 5000}])
    await svc.approve(session, po_id=po.id, actor=2)
    await svc.receive(session, po_id=po.id, actor=2, received_on=date(2026, 5, 1),
                      receipts=[{"item_id": item.id, "lot_no": "L1",
                                 "expiry_on": date(2027, 5, 1), "qty": 40}])
    assert po.status == "PARTIAL"
    await svc.receive(session, po_id=po.id, actor=2, received_on=date(2026, 5, 10),
                      receipts=[{"item_id": item.id, "lot_no": "L2",
                                 "expiry_on": date(2027, 5, 1), "qty": 60}])
    assert po.status == "RECEIVED"
    pays = (await session.execute(select(Payable).where(
        Payable.source_ref.in_([f"po-{po.id}-r1", f"po-{po.id}-r2"])))).scalars().all()
    assert sorted(float(p.amount) for p in pays) == [200000.0, 300000.0]   # 40·60 × 5,000


async def test_reject_releases_encumbrance(session):
    sup, item = await _seed(session)
    session.add(Budget(department_id=7, fiscal_year=date.today().year,
                       account_code="1150", amount=10_000_000))
    po = await svc.request(session, supplier_id=sup.id, actor=1, department_id=7,
                           lines=[{"item_id": item.id, "qty": 10, "unit_price": 5000}])
    await svc.approve(session, po_id=po.id, actor=2)
    await svc.reject(session, po_id=po.id, actor=3, reason="예산 재검토")
    await session.flush()
    assert po.status == "REJECTED"
    enc = (await session.execute(select(Encumbrance).where(
        Encumbrance.source_ref == f"po-{po.id}"))).scalar_one()
    assert enc.status == "RELEASED"
