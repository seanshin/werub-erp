"""안전재고 자동발주(par-level·P3-7) — 재주문점 미만 제안·par 기본버퍼·PR 생성·override."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.scm.models import Item, Lot, RequisitionLine
from erp.scm.replenishment_service import ReplenishmentService

svc = ReplenishmentService()


async def _item(session, code, safety, par=None):
    it = Item(code=code, name=f"품목{code}", category="SUPPLY", safety_stock_qty=safety,
              par_level=par)
    session.add(it)
    await session.flush()
    return it


async def _stock(session, item_id, qty):
    if qty > 0:
        session.add(Lot(item_id=item_id, lot_no=f"L{item_id}", expiry_on=date(2027, 1, 1),
                        received_on=date(2026, 1, 1), received_qty=qty, remaining_qty=qty))
        await session.flush()


async def test_suggestion_below_safety_uses_par(session):
    it = await _item(session, "P1", safety=10, par=50)
    await _stock(session, it.id, 4)                 # 4 < 10 → 발주, 목표 50 → 제안 46
    out = await svc.suggestions(session)
    s = next(x for x in out["items"] if x["item_id"] == it.id)
    assert s["current"] == 4 and s["suggested_qty"] == 46 and s["effective_par"] == 50


async def test_default_par_is_double_safety(session):
    it = await _item(session, "P2", safety=10)       # par 미설정 → 기본 20
    await _stock(session, it.id, 3)                   # 3 < 10 → 목표 20 → 제안 17
    out = await svc.suggestions(session)
    s = next(x for x in out["items"] if x["item_id"] == it.id)
    assert s["effective_par"] == 20 and s["suggested_qty"] == 17


async def test_above_safety_not_suggested(session):
    it = await _item(session, "P3", safety=10, par=50)
    await _stock(session, it.id, 15)                  # 15 >= 10 → 제안 제외
    out = await svc.suggestions(session)
    assert all(x["item_id"] != it.id for x in out["items"])


async def test_zero_safety_ignored(session):
    it = await _item(session, "P4", safety=0)         # 안전재고 미설정 → 대상 아님
    await _stock(session, it.id, 0)
    out = await svc.suggestions(session)
    assert all(x["item_id"] != it.id for x in out["items"])


async def test_create_pr_from_suggestions(session):
    a = await _item(session, "P5", safety=10, par=30)
    b = await _item(session, "P6", safety=5, par=20)
    await _stock(session, a.id, 2)                    # 제안 28
    await _stock(session, b.id, 1)                    # 제안 19
    req = await svc.create_pr(session, item_ids=[a.id, b.id], requester=1)
    assert req.status == "PENDING"
    lines = (await session.scalars(select(RequisitionLine).where(
        RequisitionLine.req_id == req.id))).all()
    qmap = {ln.item_id: float(ln.qty) for ln in lines}
    assert qmap == {a.id: 28, b.id: 19}


async def test_create_pr_override_qty(session):
    a = await _item(session, "P7", safety=10, par=30)
    await _stock(session, a.id, 2)
    req = await svc.create_pr(session, item_ids=[a.id], requester=1, qty_override={a.id: 100})
    lines = (await session.scalars(select(RequisitionLine).where(
        RequisitionLine.req_id == req.id))).all()
    assert float(lines[0].qty) == 100


async def test_create_pr_rejects_non_replenish(session):
    a = await _item(session, "P8", safety=10, par=30)
    await _stock(session, a.id, 50)                   # 재고 충분 → 제안 아님
    with pytest.raises(ValidationError):
        await svc.create_pr(session, item_ids=[a.id], requester=1)
