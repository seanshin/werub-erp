"""재고 워크스테이션 — 바코드 조회(lookup)·바코드 등록(patch)."""
import pytest

from erp.common.router import CurrentUser
from erp.errors import ValidationError
from erp.scm.models import Item, Lot
from erp.scm.router import ItemPatchBody, lookup_item, patch_item

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=1, license_flag=True)


async def _item(session, code="ITM1", barcode=None, safety=0.0):
    it = Item(code=code, name=f"품목{code}", category="SUPPLY", unit="EA",
              barcode=barcode, safety_stock_qty=safety)
    session.add(it)
    await session.flush()
    return it


async def test_lookup_by_barcode_returns_stock_and_lots(session):
    from datetime import date
    it = await _item(session, code="A1", barcode="880123", safety=10)
    session.add_all([
        Lot(item_id=it.id, lot_no="L1", expiry_on=date(2027, 1, 1), received_on=date(2026, 1, 1),
            received_qty=5, remaining_qty=5),
        Lot(item_id=it.id, lot_no="L2", expiry_on=date(2026, 12, 1), received_on=date(2026, 1, 1),
            received_qty=3, remaining_qty=3)])
    await session.flush()
    out = await lookup_item(q="880123", user=ADMIN, session=session)
    assert out["item"]["code"] == "A1" and out["item"]["barcode"] == "880123"
    assert out["stock"] == 8.0
    assert out["below_safety"] is True              # 8 < 10
    assert out["lots"][0]["lot_no"] == "L2"         # 유효기간 순(임박 먼저)


async def test_lookup_by_code_when_no_barcode(session):
    await _item(session, code="B1")
    out = await lookup_item(q="B1", user=ADMIN, session=session)
    assert out["item"]["code"] == "B1" and out["stock"] == 0.0


async def test_lookup_unknown_rejected(session):
    with pytest.raises(ValidationError, match="찾을 수 없습니다"):
        await lookup_item(q="NOPE", user=ADMIN, session=session)


async def test_patch_item_sets_barcode(session):
    it = await _item(session, code="C1")
    out = await patch_item(it.id, ItemPatchBody(barcode="999000"), user=ADMIN, session=session)
    assert out["barcode"] == "999000"
    again = await lookup_item(q="999000", user=ADMIN, session=session)
    assert again["item"]["id"] == it.id
