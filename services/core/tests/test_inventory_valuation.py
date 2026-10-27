"""재고자산 평가·안전재고 부족."""
from datetime import date

from erp.scm.models import Item, Lot
from erp.scm.valuation_service import ValuationService

svc = ValuationService()


async def _item(session, code, safety=0):
    it = Item(code=code, name=code, category="DRUG", safety_stock_qty=safety)
    session.add(it)
    await session.flush()
    return it


async def test_valuation_sums_remaining_times_cost(session):
    a = await _item(session, "A")
    session.add_all([
        Lot(item_id=a.id, lot_no="L1", expiry_on=date(2027, 1, 1), received_on=date(2026, 1, 1),
            received_qty=100, remaining_qty=40, unit_cost=500),
        Lot(item_id=a.id, lot_no="L2", expiry_on=date(2027, 1, 1), received_on=date(2026, 2, 1),
            received_qty=50, remaining_qty=50, unit_cost=600)])
    await session.flush()
    val = await svc.valuation(session)
    row = next(x for x in val["items"] if x["item_id"] == a.id)
    assert row["qty"] == 90 and row["value"] == 40 * 500 + 50 * 600   # 50,000
    assert val["total_value"] == 50000


async def test_valuation_flags_missing_cost(session):
    a = await _item(session, "B")
    session.add(Lot(item_id=a.id, lot_no="L1", expiry_on=date(2027, 1, 1),
                    received_on=date(2026, 1, 1), received_qty=10, remaining_qty=10, unit_cost=None))
    await session.flush()
    val = await svc.valuation(session)
    assert next(x for x in val["items"] if x["item_id"] == a.id)["cost_missing"]


async def test_shortage_lists_below_safety(session):
    a = await _item(session, "C", safety=100)
    session.add(Lot(item_id=a.id, lot_no="L1", expiry_on=date(2027, 1, 1),
                    received_on=date(2026, 1, 1), received_qty=30, remaining_qty=30, unit_cost=100))
    b = await _item(session, "D", safety=10)
    session.add(Lot(item_id=b.id, lot_no="L2", expiry_on=date(2027, 1, 1),
                    received_on=date(2026, 1, 1), received_qty=50, remaining_qty=50, unit_cost=100))
    await session.flush()
    sh = await svc.shortage(session)
    codes = {x["code"] for x in sh["items"]}
    assert "C" in codes and "D" not in codes   # C 부족(30<100), D 충분(50>10)
    assert next(x for x in sh["items"] if x["code"] == "C")["shortage"] == 70
