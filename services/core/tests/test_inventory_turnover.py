"""재고회전율·장기재고."""
from datetime import date

from erp.scm.models import Item, Lot, StockMovement
from erp.scm.valuation_service import ValuationService

svc = ValuationService()


async def _item(session, code):
    it = Item(code=code, name=code, category="SUPPLY", safety_stock_qty=0)
    session.add(it)
    await session.flush()
    return it


async def test_turnover_and_dead_stock(session):
    a = await _item(session, "A")
    b = await _item(session, "B")
    lot_a = Lot(item_id=a.id, lot_no="LA", expiry_on=date(2027, 1, 1), received_on=date(2026, 1, 1),
                received_qty=100, remaining_qty=50, unit_cost=100)
    session.add_all([
        lot_a,
        Lot(item_id=b.id, lot_no="LB", expiry_on=date(2027, 1, 1), received_on=date(2026, 1, 1),
            received_qty=20, remaining_qty=20, unit_cost=100)])  # B: 출고 없음 → dead
    await session.flush()
    # A 출고 100 (5~6월) — 실제 lot id 참조(PG는 시퀀스가 테스트간 리셋 안 됨 → 하드코딩 1 금지)
    session.add(StockMovement(item_id=a.id, lot_id=lot_a.id, movement_type="OUT", qty=100,
                              moved_on=date(2026, 5, 15), actor=1))
    await session.flush()
    out = await svc.turnover(session, since=date(2026, 5, 1), until=date(2026, 6, 30))
    ra = next(x for x in out["items"] if x["item_id"] == a.id)
    rb = next(x for x in out["items"] if x["item_id"] == b.id)
    assert ra["out_qty"] == 100 and ra["current_stock"] == 50 and ra["turnover"] == 2.0
    assert rb["dead_stock"] and rb["out_qty"] == 0
    assert out["dead_stock_count"] == 1
