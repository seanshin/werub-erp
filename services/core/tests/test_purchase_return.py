"""입고 검수·반품(RMA·0214) — 로트 차감·credit note(DR 2110/CR 1150)·미지급 차감·교체 재입고·잔량 가드."""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.errors import ValidationError
from erp.finance.models import Payable, Voucher, VoucherLine
from erp.scm.models import Item, Lot, PurchaseReturn, StockMovement, Supplier
from erp.scm.po_service import PurchaseOrderService
from erp.scm.return_service import PurchaseReturnService

po_svc = PurchaseOrderService()
ret_svc = PurchaseReturnService()
D = date(2026, 5, 1)
RD = date(2026, 5, 10)


async def _received_po(session, *, qty=100, price=5000):
    sup = Supplier(name="대한약품", business_no="1018212345")
    item = Item(code="M001", name="수액세트", category="SUPPLY", is_narcotic=False,
                safety_stock_qty=10)
    session.add_all([sup, item])
    await session.flush()
    po = await po_svc.request(session, supplier_id=sup.id, actor=1,
                              lines=[{"item_id": item.id, "qty": qty, "unit_price": price}])
    await po_svc.approve(session, po_id=po.id, actor=2)
    await po_svc.receive(session, po_id=po.id, actor=2, received_on=D,
                         receipts=[{"item_id": item.id, "lot_no": "L1",
                                    "expiry_on": date(2027, 5, 1), "qty": qty}])
    lot = await session.scalar(select(Lot).where(Lot.item_id == item.id))
    return po, item, lot


async def test_return_reduces_lot_and_posts_credit_note(session):
    po, item, lot = await _received_po(session)          # 100개 @5000 = 500,000 입고
    ret = await ret_svc.create_return(session, po_id=po.id, lot_id=lot.id, qty=30,
                                      reason="DAMAGE", returned_on=RD, actor=2)
    assert float(ret.amount) == 150_000
    # 로트 잔량 차감 + RETURN 이동
    assert float(lot.remaining_qty) == 70
    mv = await session.scalar(select(StockMovement).where(
        StockMovement.movement_type == "RETURN"))
    assert float(mv.qty) == 30
    # credit note — DR 2110 / CR 1150
    lines = (await session.execute(select(VoucherLine).join(
        Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.source_ref == f"po-return-{ret.id}"))).scalars().all()
    assert {(x.account_code, x.side, float(x.amount)) for x in lines} == {
        ("2110", "DEBIT", 150_000), ("1150", "CREDIT", 150_000)}
    # 미지급 차감 — 입고 50만 → 반품 15만 → 잔액 35만
    pay = await session.scalar(select(Payable).where(
        Payable.source_ref == f"po-{po.id}-r1"))
    assert float(pay.amount) == 350_000


async def test_return_reopens_po_for_replacement(session):
    """반품으로 received_qty 감소 → PO RECEIVED에서 PARTIAL로(교체 재입고 재개)."""
    po, item, lot = await _received_po(session, qty=100)
    assert po.status == "RECEIVED"
    await ret_svc.create_return(session, po_id=po.id, lot_id=lot.id, qty=40,
                                reason="DEFECT", returned_on=RD, actor=2)
    assert po.status == "PARTIAL"
    # 교체분 재입고 가능(3-way가 반품분만큼 여유 확보)
    await po_svc.receive(session, po_id=po.id, actor=2, received_on=RD,
                         receipts=[{"item_id": item.id, "lot_no": "L1R",
                                    "expiry_on": date(2027, 6, 1), "qty": 40}])
    assert po.status == "RECEIVED"


async def test_return_exceeding_remaining_blocked(session):
    po, item, lot = await _received_po(session, qty=50)
    with pytest.raises(ValidationError, match="로트 잔량"):
        await ret_svc.create_return(session, po_id=po.id, lot_id=lot.id, qty=60,
                                    reason="EXPIRY", returned_on=RD, actor=2)


async def test_return_invalid_reason_and_qty(session):
    po, item, lot = await _received_po(session, qty=50)
    with pytest.raises(ValidationError, match="사유"):
        await ret_svc.create_return(session, po_id=po.id, lot_id=lot.id, qty=5,
                                    reason="NOPE", returned_on=RD, actor=2)
    with pytest.raises(ValidationError, match="0보다 커야"):
        await ret_svc.create_return(session, po_id=po.id, lot_id=lot.id, qty=0,
                                    reason="OTHER", returned_on=RD, actor=2)


async def test_return_after_consumption_limited_to_remaining(session):
    """일부 소비 후 반품 — 잔량 이내만(소비분 반품 불가)."""
    from erp.scm.stock_service import StockService
    po, item, lot = await _received_po(session, qty=100)
    await StockService().issue(session, item_id=item.id, qty=80, moved_on=D, actor=2)
    assert float(lot.remaining_qty) == 20
    with pytest.raises(ValidationError, match="로트 잔량"):
        await ret_svc.create_return(session, po_id=po.id, lot_id=lot.id, qty=30,
                                    reason="DAMAGE", returned_on=RD, actor=2)
    ret = await ret_svc.create_return(session, po_id=po.id, lot_id=lot.id, qty=20,
                                      reason="DAMAGE", returned_on=RD, actor=2)
    assert float(lot.remaining_qty) == 0 and float(ret.amount) == 100_000


async def test_return_landed_cost_variance_to_5540(session):
    """F4 — WAVG 이동평균이 lot.unit_cost를 재기입해 장부가≠PO단가일 때, 공급사 AP는 청구단가로
    감액하고 재고는 장부가로 반제, 차액은 재고평가손익(5540)."""
    from erp.scm.stock_service import StockService
    sup = Supplier(name="대한약품", business_no="1018212345")
    item = Item(code="M001", name="스텐트", category="SUPPLY", is_narcotic=False,
                safety_stock_qty=1, cost_method="WAVG")
    session.add_all([sup, item])
    await session.flush()
    # PO-A 100개 @5000, PO-B 100개 @7000 — 같은 품목 두 로트
    poA = await po_svc.request(session, supplier_id=sup.id, actor=1,
                               lines=[{"item_id": item.id, "qty": 100, "unit_price": 5000}])
    await po_svc.approve(session, po_id=poA.id, actor=2)
    await po_svc.receive(session, po_id=poA.id, actor=2, received_on=D,
                         receipts=[{"item_id": item.id, "lot_no": "LA",
                                    "expiry_on": date(2027, 5, 1), "qty": 100}])
    poB = await po_svc.request(session, supplier_id=sup.id, actor=1,
                               lines=[{"item_id": item.id, "qty": 100, "unit_price": 7000}])
    await po_svc.approve(session, po_id=poB.id, actor=2)
    await po_svc.receive(session, po_id=poB.id, actor=2, received_on=D,
                         receipts=[{"item_id": item.id, "lot_no": "LB",
                                    "expiry_on": date(2027, 6, 1), "qty": 100}])
    # WAVG 출고 1개 → 잔여 로트 unit_cost가 이동평균 6000으로 재기입
    await StockService().issue(session, item_id=item.id, qty=1, moved_on=D, actor=2)
    lotA = await session.scalar(select(Lot).where(Lot.lot_no == "LA"))
    assert float(lotA.unit_cost) == 6000            # 장부가 재기입(5000→6000)
    # 로트A(장부 6000·PO단가 5000)에서 10개 반품
    ret = await ret_svc.create_return(session, po_id=poA.id, lot_id=lotA.id, qty=10,
                                      reason="DEFECT", returned_on=RD, actor=2)
    assert float(ret.amount) == 60_000              # 재고 반제 = 장부가 10×6000
    lines = {(x.account_code, x.side): float(x.amount) for x in (await session.execute(
        select(VoucherLine).join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.source_ref == f"po-return-{ret.id}"))).scalars().all()}
    assert lines[("1150", "CREDIT")] == 60_000      # 재고 장부가 반제
    assert lines[("2110", "DEBIT")] == 50_000       # 공급사 AP = 청구단가 10×5000
    assert lines[("5540", "DEBIT")] == 10_000       # 장부>청구 차액 = 평가손실
    # 미지급 감액도 청구단가 기준(입고 50만 → 반품 5만 → 45만)
    pay = await session.scalar(select(Payable).where(Payable.source_ref == f"po-{poA.id}-r1"))
    assert float(pay.amount) == 450_000


async def test_credit_note_balances(session):
    """반품 전표 차대 균형."""
    po, item, lot = await _received_po(session, qty=10, price=3000)
    ret = await ret_svc.create_return(session, po_id=po.id, lot_id=lot.id, qty=10,
                                      reason="WRONG_ITEM", returned_on=RD, actor=2)
    dr = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).join(
        Voucher, Voucher.id == VoucherLine.voucher_id).where(
        Voucher.source_ref == f"po-return-{ret.id}", VoucherLine.side == "DEBIT"))
    cr = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).join(
        Voucher, Voucher.id == VoucherLine.voucher_id).where(
        Voucher.source_ref == f"po-return-{ret.id}", VoucherLine.side == "CREDIT"))
    assert float(dr) == float(cr) == 30_000
    assert await session.scalar(select(func.count()).select_from(PurchaseReturn)) == 1
