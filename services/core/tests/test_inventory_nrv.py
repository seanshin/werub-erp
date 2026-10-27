"""재고 저가법 NRV (K-IFRS 1002) — 장부가 vs NRV 평가손실·충당 결산분개·멱등."""
from datetime import date

from erp.finance.models import Account
from erp.scm.models import Item, Lot
from erp.scm.valuation_service import ValuationService

svc = ValuationService()


async def _seed(session):
    session.add_all([
        Account(code="1159", name="재고자산평가충당금", kind="ASSET"),
        Account(code="5540", name="재고자산평가손실", kind="EXPENSE"),
    ])
    # 품목 A: 잔량 100 × 원가 1,000 = 장부가 100,000, NRV단가 700 → NRV 70,000 → 손실 30,000
    a = Item(code="A1", name="약품A", category="DRUG", nrv_unit=700)
    # 품목 B: 원가 500, NRV단가 600(원가보다 높음) → 손실 0
    b = Item(code="B1", name="약품B", category="SUPPLY", nrv_unit=600)
    # 품목 C: NRV 미설정 → 제외
    c = Item(code="C1", name="약품C", category="SUPPLY")
    session.add_all([a, b, c])
    await session.flush()
    session.add_all([
        Lot(item_id=a.id, lot_no="LA", expiry_on=date(2027, 1, 1), received_on=date(2026, 1, 1),
            received_qty=100, remaining_qty=100, unit_cost=1000),
        Lot(item_id=b.id, lot_no="LB", expiry_on=date(2027, 1, 1), received_on=date(2026, 1, 1),
            received_qty=50, remaining_qty=50, unit_cost=500),
        Lot(item_id=c.id, lot_no="LC", expiry_on=date(2027, 1, 1), received_on=date(2026, 1, 1),
            received_qty=10, remaining_qty=10, unit_cost=300),
    ])
    await session.flush()


async def test_nrv_preview(session):
    await _seed(session)
    p = await svc.nrv_preview(session, yyyymm="2026-06")
    # A만 손실 30,000 / B·C 제외
    assert p["required"] == 30000
    assert len(p["items"]) == 1 and p["items"][0]["code"] == "A1"
    assert p["items"][0]["wavg_unit"] == 1000.0
    assert p["delta"] == 30000


async def test_nrv_excludes_null_cost_lot(session):
    """원가 미상 lot은 NRV 평가서 제외 — qty(전량)/book(원가有) 불일치 손실 과소 방지(전체검토 scm)."""
    a = Item(code="AN", name="혼합원가", category="DRUG", nrv_unit=700)
    session.add(a)
    await session.flush()
    session.add_all([
        Lot(item_id=a.id, lot_no="C1", expiry_on=date(2027, 1, 1), received_on=date(2026, 1, 1),
            received_qty=100, remaining_qty=100, unit_cost=1000),     # 원가 有
        Lot(item_id=a.id, lot_no="N1", expiry_on=date(2027, 1, 1), received_on=date(2026, 1, 2),
            received_qty=50, remaining_qty=50, unit_cost=None),       # 원가 미상 → 제외
    ])
    await session.flush()
    p = await svc.nrv_preview(session, yyyymm="2026-06")
    it = next(i for i in p["items"] if i["code"] == "AN")
    # 원가 있는 100개만: 장부 100,000, NRV 100×700=70,000 → 손실 30,000 (null lot 50개 미포함)
    assert it["qty"] == 100 and it["book_value"] == 100000 and it["loss"] == 30000


async def test_nrv_post_idempotent(session):
    await _seed(session)
    r1 = await svc.nrv_post(session, yyyymm="2026-06")
    assert r1["posted"] and r1["delta"] == 30000
    r2 = await svc.nrv_post(session, yyyymm="2026-06")
    assert r2["posted"] is False and r2["delta"] == 0
    bal = await svc._allowance_balance(session, as_of=date(2026, 6, 30))
    assert bal == 30000
