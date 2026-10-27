"""재고 출고원가 COGS (K-IFRS 1002) — FIFO vs 가중평균(WAVG) 원가흐름."""
from datetime import date

from erp.scm.models import Item
from erp.scm.stock_service import StockService
from erp.scm.valuation_service import ValuationService

stock = StockService()
val = ValuationService()


async def _item(session, code, method):
    it = Item(code=code, name=code, category="SUPPLY", cost_method=method)
    session.add(it)
    await session.flush()
    # 두 lot: 100개 @1,000, 100개 @1,400 (유효기간 동일 → 입고일 순 FIFO)
    await stock.receive(session, item_id=it.id, lot_no="L1", qty=100,
                        expiry_on=date(2027, 1, 1), moved_on=date(2026, 1, 1), unit_cost=1000, actor=1)
    await stock.receive(session, item_id=it.id, lot_no="L2", qty=100,
                        expiry_on=date(2027, 1, 1), moved_on=date(2026, 2, 1), unit_cost=1400, actor=1)
    return it


async def test_fifo_cogs(session):
    it = await _item(session, "F1", "FIFO")
    # 150 출고 → FIFO: 100×1000 + 50×1400 = 170,000
    mvs = await stock.issue(session, item_id=it.id, qty=150, moved_on=date(2026, 3, 1), actor=1)
    assert round(sum(float(m.cost) for m in mvs)) == 170000
    c = await val.cogs(session, since=date(2026, 3, 1), until=date(2026, 3, 31))
    assert c["total_cogs"] == 170000


async def test_issue_posts_cogs_voucher(session):
    """출고 시 매출원가 자동전기 — 재료비(5410)/재고자산(1150) 전표(전체검토 결정)."""
    from sqlalchemy import select

    from erp.finance.models import Voucher, VoucherLine
    it = await _item(session, "V1", "FIFO")
    mvs = await stock.issue(session, item_id=it.id, qty=150, moved_on=date(2026, 3, 1), actor=1)
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == f"stock-issue:{mvs[0].id}"))
    assert v is not None
    lines = {(ln.account_code, ln.side): float(ln.amount) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert lines[("5410", "DEBIT")] == 170000 and lines[("1150", "CREDIT")] == 170000


async def test_wavg_cogs(session):
    it = await _item(session, "W1", "WAVG")
    # 가중평균단가 = (100*1000+100*1400)/200 = 1,200. 150 출고 → 180,000
    mvs = await stock.issue(session, item_id=it.id, qty=150, moved_on=date(2026, 3, 1), actor=1)
    assert round(sum(float(m.cost) for m in mvs)) == 180000
    c = await val.cogs(session, since=date(2026, 3, 1), until=date(2026, 3, 31))
    assert c["items"][0]["cost_method"] == "WAVG" and c["total_cogs"] == 180000


async def test_wavg_remaining_revalued_to_moving_average(session):
    """심층 재검토 — WAVG 출고 후 잔여 재고를 이동평균단가로 이월(GL relief와 재고평가 정합).

    미이월(구버전)이면 출고는 wavg 1,200으로 relief하나 잔여는 원가 lot 1,400으로 평가돼
    영구 괴리(GL↔보조부). FIFO 품목은 실 lot원가 유지(무영향·test_fifo_cogs)."""
    from sqlalchemy import select

    from erp.scm.models import Lot
    it = await _item(session, "WR", "WAVG")    # 100@1000 + 100@1400 → wavg 1,200
    await stock.issue(session, item_id=it.id, qty=150, moved_on=date(2026, 3, 1), actor=1)
    remaining = [lot for lot in (await session.execute(
        select(Lot).where(Lot.item_id == it.id))).scalars() if float(lot.remaining_qty) > 0]
    assert sum(float(lot.remaining_qty) for lot in remaining) == 50
    assert all(float(lot.unit_cost) == 1200 for lot in remaining)   # 원가 1,400 아닌 이동평균 1,200
    # 재고평가 = 50×1,200 = 60,000 = 수입 240,000 − COGS relief 180,000 (GL과 정합)
    assert sum(float(lot.remaining_qty) * float(lot.unit_cost) for lot in remaining) == 60000
