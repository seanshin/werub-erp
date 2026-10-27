"""의약품 회수·의료폐기물 대장 (규제·안전 logging)."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.scm.models import Item, Lot
from erp.scm.safety_service import DrugRecallService, MedicalWasteService

recall = DrugRecallService()
waste = MedicalWasteService()


async def test_drug_recall_identifies_affected_lots(session):
    item = Item(code="D1", name="회수약품", category="DRUG")
    session.add(item)
    await session.flush()
    session.add_all([
        Lot(item_id=item.id, lot_no="L1", expiry_on=date(2027, 1, 1),
            received_on=date(2026, 1, 1), received_qty=100, remaining_qty=80),
        Lot(item_id=item.id, lot_no="L2", expiry_on=date(2027, 1, 1),
            received_on=date(2026, 1, 1), received_qty=50, remaining_qty=0),   # 소진 — 제외
    ])
    await session.flush()
    out = await recall.register(session, item_id=item.id, reason="이물 혼입",
                                recall_on=date(2026, 6, 20), severity="CLASS2")
    assert out["affected_qty"] == 80.0 and len(out["affected_lots"]) == 1
    assert out["affected_lots"][0]["lot_no"] == "L1"


async def test_waste_log_lifecycle(session):
    w = await waste.record(session, waste_type="격리", weight_kg=12.5, stored_on=date(2026, 6, 1))
    assert w.status == "STORED"
    w2 = await waste.hand_over(session, log_id=w.id, handed_over_on=date(2026, 6, 5),
                               handler="A환경", manifest_no="OLB-2026-0001")
    assert w2.status == "HANDED_OVER" and w2.manifest_no == "OLB-2026-0001"


async def test_waste_weight_guard(session):
    with pytest.raises(ValidationError):
        await waste.record(session, waste_type="일반", weight_kg=0, stored_on=date(2026, 6, 1))


async def test_list_recalls_and_close(session):
    item = Item(code="D9", name="회수약", category="DRUG")
    session.add(item)
    await session.flush()
    r = await recall.register(session, item_id=item.id, reason="라벨오류",
                              recall_on=date(2026, 6, 20), severity="VOLUNTARY")
    rows = await recall.list_recalls(session)
    assert any(x["id"] == r["id"] and x["status"] == "OPEN" for x in rows)
    await recall.close(session, recall_id=r["id"])
    assert await recall.list_recalls(session, status="OPEN") == \
        [x for x in await recall.list_recalls(session, status="OPEN") if x["id"] != r["id"]]
    closed = await recall.list_recalls(session, status="CLOSED")
    assert any(x["id"] == r["id"] for x in closed)


async def test_list_waste_logs(session):
    w = await waste.record(session, waste_type="위해", weight_kg=5.0, stored_on=date(2026, 6, 1))
    stored = await waste.list_logs(session, status="STORED")
    assert any(x["id"] == w.id and x["status"] == "STORED" for x in stored)
    await waste.hand_over(session, log_id=w.id, handed_over_on=date(2026, 6, 5), handler="B환경")
    assert any(x["id"] == w.id for x in await waste.list_logs(session, status="HANDED_OVER"))
