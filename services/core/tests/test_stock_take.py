"""재고 실사 — 부족(감모손실)·과잉(조정이익) 조정·전표."""
from datetime import date

from sqlalchemy import select

from erp.finance.models import Account, Voucher
from erp.scm.models import Item, Lot
from erp.scm.stocktake_service import StockTakeService

svc = StockTakeService()


async def _seed(session, qty, cost=500):
    session.add_all([
        Account(code="1150", name="저장품", kind="ASSET"),
        Account(code="5530", name="재고감모손실", kind="EXPENSE"),
        Account(code="4620", name="재고조정이익", kind="REVENUE")])
    it = Item(code="X", name="X", category="SUPPLY", safety_stock_qty=0)
    session.add(it)
    await session.flush()
    session.add(Lot(item_id=it.id, lot_no="L1", expiry_on=date(2027, 1, 1),
                    received_on=date(2026, 1, 1), received_qty=qty, remaining_qty=qty, unit_cost=cost))
    await session.flush()
    return it


async def test_shortage_creates_shrink_voucher(session):
    it = await _seed(session, 100, 500)
    st = await svc.record(session, item_id=it.id, physical_qty=90, taken_on=date(2026, 6, 1))
    assert st.book_qty == 100 and st.diff_qty == -10 and st.diff_value == -5000
    out = await svc.apply(session, stocktake_id=st.id)
    assert out["status"] == "APPLIED"
    # 잔량 90으로 차감
    remain = float(await session.scalar(select(Lot.remaining_qty).where(Lot.item_id == it.id)))
    assert remain == 90
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == f"stocktake:{st.id}"))
    assert v is not None


async def test_overage_creates_gain_and_lot(session):
    it = await _seed(session, 50, 400)
    st = await svc.record(session, item_id=it.id, physical_qty=60, taken_on=date(2026, 6, 1))
    assert st.diff_qty == 10 and st.diff_value == 4000
    await svc.apply(session, stocktake_id=st.id)
    total = float(await session.scalar(
        select(__import__("sqlalchemy").func.sum(Lot.remaining_qty)).where(Lot.item_id == it.id)))
    assert total == 60   # 조정 로트 +10


async def test_shortage_voucher_uses_actual_fifo_lot_cost(session):
    """부족 감모 전표는 가중평균이 아닌 **실제 차감 FIFO 로트원가** 합이어야 GL=영구재고 일치(전체검토 2b)."""
    from erp.finance.models import VoucherLine
    session.add_all([
        Account(code="1150", name="저장품", kind="ASSET"),
        Account(code="5530", name="재고감모손실", kind="EXPENSE"),
        Account(code="4620", name="재고조정이익", kind="REVENUE")])
    it = Item(code="MZ", name="다단가", category="SUPPLY", safety_stock_qty=0)
    session.add(it)
    await session.flush()
    # 로트A 50@100(선소진) + 로트B 50@300 → WAVG=200
    session.add_all([
        Lot(item_id=it.id, lot_no="A", expiry_on=date(2026, 6, 1), received_on=date(2026, 1, 1),
            received_qty=50, remaining_qty=50, unit_cost=100),
        Lot(item_id=it.id, lot_no="B", expiry_on=date(2027, 6, 1), received_on=date(2026, 1, 2),
            received_qty=50, remaining_qty=50, unit_cost=300)])
    await session.flush()
    st = await svc.record(session, item_id=it.id, physical_qty=40, taken_on=date(2026, 6, 1))
    out = await svc.apply(session, stocktake_id=st.id)   # 부족 60 → FIFO 50@100 + 10@300
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == f"stocktake:{st.id}"))
    shrink = await session.scalar(select(VoucherLine.amount).where(
        VoucherLine.voucher_id == v.id, VoucherLine.account_code == "5530"))
    assert float(shrink) == 8000      # 50*100 + 10*300 (WAVG였다면 60*200=12000 — 오류)
    assert out["status"] == "APPLIED"


async def test_apply_idempotent(session):
    it = await _seed(session, 100, 500)
    st = await svc.record(session, item_id=it.id, physical_qty=80, taken_on=date(2026, 6, 1))
    o1 = await svc.apply(session, stocktake_id=st.id)
    o2 = await svc.apply(session, stocktake_id=st.id)
    assert o1["status"] == o2["status"] == "APPLIED"
    remain = float(await session.scalar(select(Lot.remaining_qty).where(Lot.item_id == it.id)))
    assert remain == 80   # 재적용 안 함(이중 차감 X)
