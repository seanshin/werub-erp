"""위탁재고(Consignment·0219) — 적재 무회계·사용 매입(DR5410/CR2110)·반품 무회계·잔량 가드."""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.errors import ValidationError
from erp.finance.models import Account, Payable, Voucher, VoucherLine
from erp.scm.consignment_service import ConsignmentService
from erp.scm.models import ConsignmentConsumption, Item, Supplier

svc = ConsignmentService()
D = date(2026, 7, 21)


async def _seed(session):
    session.add_all([
        Account(code="5410", name="재료비", kind="EXPENSE"),
        Account(code="2110", name="미지급금", kind="LIABILITY")])
    sup = Supplier(name="정형외과상사", business_no="1112223333")
    item = Item(code="IMP1", name="인공관절", category="SUPPLY", is_narcotic=False,
                safety_stock_qty=0)
    session.add_all([sup, item])
    await session.flush()
    return sup, item


async def test_place_has_no_gl(session):
    """적재는 회계 없음(미소유) — 위탁 잔량만 기록."""
    sup, item = await _seed(session)
    cs = await svc.place(session, supplier_id=sup.id, item_id=item.id, lot_no="L1", qty=5,
                         unit_cost=2_000_000, placed_on=D)
    assert cs.status == "ACTIVE" and float(cs.qty_on_hand) == 5
    assert await session.scalar(select(func.count()).select_from(Voucher)) == 0
    assert await session.scalar(select(func.count()).select_from(Payable)) == 0


async def test_consume_recognizes_purchase_and_payable(session):
    """사용 → 소유권 이전·DR 5410 재료비/CR 2110 미지급금·미지급 발생(party_ref)."""
    sup, item = await _seed(session)
    cs = await svc.place(session, supplier_id=sup.id, item_id=item.id, lot_no="L1", qty=5,
                         unit_cost=2_000_000, placed_on=D)
    con = await svc.consume(session, consignment_id=cs.id, qty=2, consumed_on=D,
                            chart_no="C-1", actor=1)
    assert float(con.amount) == 4_000_000 and float(cs.qty_on_hand) == 3
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == f"ap-bill-{(await session.scalar(select(Payable))).id}"))
    lines = (await session.scalars(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id))).all()
    assert {(x.account_code, x.side, float(x.amount)) for x in lines} == {
        ("5410", "DEBIT", 4_000_000), ("2110", "CREDIT", 4_000_000)}
    pay = await session.scalar(select(Payable))
    assert pay.party_ref == f"supplier:{sup.id}" and float(pay.amount) == 4_000_000


async def test_consume_idempotent_on_client_ref(session):
    """F5 — 동일 client_ref 재전송은 기존 사용 반환(잔량 이중차감·미지급 중복 없음)."""
    sup, item = await _seed(session)
    cs = await svc.place(session, supplier_id=sup.id, item_id=item.id, lot_no="L1", qty=5,
                         unit_cost=2_000_000, placed_on=D)
    con1 = await svc.consume(session, consignment_id=cs.id, qty=2, consumed_on=D, actor=1,
                             client_ref="POS-abc-1")
    con2 = await svc.consume(session, consignment_id=cs.id, qty=2, consumed_on=D, actor=1,
                             client_ref="POS-abc-1")     # 재전송
    assert con2.id == con1.id                            # 기존 레코드 반환
    assert float(cs.qty_on_hand) == 3                    # 한 번만 차감
    assert await session.scalar(select(func.count()).select_from(Payable)) == 1
    assert await session.scalar(
        select(func.count()).select_from(ConsignmentConsumption)) == 1


async def test_consume_depletes_and_blocks_over(session):
    sup, item = await _seed(session)
    cs = await svc.place(session, supplier_id=sup.id, item_id=item.id, lot_no="L1", qty=2,
                         unit_cost=1_000_000, placed_on=D)
    with pytest.raises(ValidationError, match="위탁 잔량"):
        await svc.consume(session, consignment_id=cs.id, qty=3, consumed_on=D)
    await svc.consume(session, consignment_id=cs.id, qty=2, consumed_on=D, actor=1)
    assert cs.status == "DEPLETED" and float(cs.qty_on_hand) == 0
    with pytest.raises(ValidationError, match="사용 가능한"):     # DEPLETED 재사용 불가
        await svc.consume(session, consignment_id=cs.id, qty=1, consumed_on=D)


async def test_return_no_gl_and_status(session):
    """미사용 반품 — 회계 없음·잔량 소진 시 RETURNED."""
    sup, item = await _seed(session)
    cs = await svc.place(session, supplier_id=sup.id, item_id=item.id, lot_no="L1", qty=4,
                         unit_cost=500_000, placed_on=D)
    await svc.return_stock(session, consignment_id=cs.id, qty=4, returned_on=D, actor=1)
    assert cs.status == "RETURNED" and float(cs.qty_on_hand) == 0
    assert await session.scalar(select(func.count()).select_from(Voucher)) == 0
    assert await session.scalar(select(func.count()).select_from(Payable)) == 0


async def test_consumptions_summary(session):
    sup, item = await _seed(session)
    cs = await svc.place(session, supplier_id=sup.id, item_id=item.id, lot_no="L1", qty=10,
                         unit_cost=100_000, placed_on=D)
    await svc.consume(session, consignment_id=cs.id, qty=2, consumed_on=D, actor=1)
    await svc.consume(session, consignment_id=cs.id, qty=3, consumed_on=D, actor=1)
    out = await svc.list_consumptions(session, since=date(2026, 7, 1), until=date(2026, 7, 31))
    assert out["total_amount"] == 500_000 and len(out["items"]) == 2


async def test_place_validation(session):
    sup, item = await _seed(session)
    with pytest.raises(ValidationError, match="0보다 커야"):
        await svc.place(session, supplier_id=sup.id, item_id=item.id, lot_no="L", qty=0,
                        unit_cost=100, placed_on=D)
    with pytest.raises(ValidationError, match="공급사"):
        await svc.place(session, supplier_id=999, item_id=item.id, lot_no="L", qty=1,
                        unit_cost=100, placed_on=D)


# ---- chart_no 조달 스코프 노출 축소(Phase0 규율) ----

async def _seed_consumption(session, *, chart_no: str):
    sup, item = await _seed(session)
    cs = await svc.place(session, supplier_id=sup.id, item_id=item.id, lot_no="L1", qty=10,
                         unit_cost=100_000, placed_on=D)
    await svc.consume(session, consignment_id=cs.id, qty=1, consumed_on=D,
                      chart_no=chart_no, actor=1)


async def test_consumption_chart_no_masked_for_procurement(session, client):
    """물류관리자(scm.po read O · patient read X)는 마스킹된 차트번호만 본다."""
    from tests._http import auth_as
    await _seed_consumption(session, chart_no="P2026-0005")
    logm = await auth_as(session, user_id=91, role="LOGISTICS_MANAGER")
    r = await client.get("/api/v1/inventory/consignment-consumptions", headers=logm)
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["chart_no"] == "P2***05" and item["chart_masked"] is True


async def test_consumption_chart_no_plain_for_patient_authorized(session, client):
    """환자 식별 권한자(ADMIN)는 원문 — 청구 대사가 가능해야 한다."""
    from tests._http import auth_as
    await _seed_consumption(session, chart_no="P2026-0005")
    admin = await auth_as(session, user_id=92, role="ADMIN")
    r = await client.get("/api/v1/inventory/consignment-consumptions", headers=admin)
    item = r.json()["items"][0]
    assert item["chart_no"] == "P2026-0005" and item["chart_masked"] is False
