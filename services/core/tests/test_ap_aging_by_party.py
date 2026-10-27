"""거래처별 AP aging(0216) — party_ref 그룹핑·버킷 분류·이름 폴백·PO 입고 party_ref 배선."""
from datetime import date

from erp.finance.models import Payable
from erp.finance.settlement_service import SettlementService

svc = SettlementService()
ASOF = date(2026, 7, 21)


def _pay(party, amount, due, *, ref=None, paid=0):
    return Payable(party=party, amount=amount, paid_amount=paid, incurred_on=date(2026, 5, 1),
                   due_on=due, party_ref=ref, source_module="scm",
                   status="PARTIAL" if paid else "OUTSTANDING")


async def test_aging_by_party_matrix(session):
    """거래처별 × 버킷 — party_ref 그룹핑·경과일 분류·총잔액·최근 지급예정일."""
    session.add_all([
        _pay("대한약품", 100_000, date(2026, 7, 25), ref="supplier:1"),   # current(미도래)
        _pay("대한약품", 50_000, date(2026, 6, 1), ref="supplier:1"),     # 61-90 (50일? 계산)
        _pay("한빛의료", 30_000, date(2026, 4, 1), ref="supplier:2"),     # 90+
    ])
    await session.flush()
    out = await svc.aging_by_party(session, as_of=ASOF, kind="AP")
    by = {p["party"]: p for p in out["parties"]}
    assert by["대한약품"]["total"] == 150_000
    assert by["대한약품"]["party_ref"] == "supplier:1"
    assert by["대한약품"]["buckets"]["current"] == 100_000     # 7/25 미도래
    assert by["대한약품"]["next_due"] == "2026-06-01"          # 최근 지급예정
    assert by["한빛의료"]["buckets"]["90+"] == 30_000          # 4/1 → 111일 경과
    assert out["total"] == 180_000
    # 총잔액 desc 정렬
    assert out["parties"][0]["party"] == "대한약품"


async def test_same_name_different_ref_separated(session):
    """동명 거래처라도 party_ref가 다르면 분리(마스터 안정키)."""
    session.add_all([
        _pay("메디칼", 10_000, date(2026, 7, 1), ref="supplier:10"),
        _pay("메디칼", 20_000, date(2026, 7, 1), ref="supplier:11"),
    ])
    await session.flush()
    out = await svc.aging_by_party(session, as_of=ASOF, kind="AP")
    refs = {p["party_ref"]: p["total"] for p in out["parties"]}
    assert refs == {"supplier:10": 10_000, "supplier:11": 20_000}


async def test_name_fallback_when_no_ref(session):
    """party_ref 없는 레거시 행은 이름으로 그룹핑(폴백)."""
    session.add_all([
        _pay("구거래처", 5_000, date(2026, 7, 1)),
        _pay("구거래처", 7_000, date(2026, 7, 1)),
    ])
    await session.flush()
    out = await svc.aging_by_party(session, as_of=ASOF, kind="AP")
    assert len(out["parties"]) == 1 and out["parties"][0]["total"] == 12_000


async def test_paid_excluded_partial_outstanding_only(session):
    """PAID 제외·PARTIAL은 미결제 잔액만."""
    session.add(_pay("대한약품", 100_000, date(2026, 7, 1), ref="supplier:1", paid=70_000))
    fully = _pay("대한약품", 40_000, date(2026, 7, 1), ref="supplier:1", paid=40_000)
    fully.status = "PAID"
    session.add(fully)
    await session.flush()
    out = await svc.aging_by_party(session, as_of=ASOF, kind="AP")
    assert out["total"] == 30_000     # 100k-70k, PAID 40k 제외


async def test_po_receive_wires_party_ref(session):
    """PO 입고 미지급이 party_ref=supplier:{id}로 기록(거래처 마스터 연계)."""
    from sqlalchemy import select

    from erp.scm.models import Item, Supplier
    from erp.scm.po_service import PurchaseOrderService
    po_svc = PurchaseOrderService()
    sup = Supplier(name="대한약품", business_no="1018212345")
    item = Item(code="M1", name="수액", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add_all([sup, item])
    await session.flush()
    po = await po_svc.request(session, supplier_id=sup.id, actor=1,
                              lines=[{"item_id": item.id, "qty": 10, "unit_price": 5000}])
    await po_svc.approve(session, po_id=po.id, actor=2)
    await po_svc.receive(session, po_id=po.id, actor=2, received_on=date(2026, 5, 1),
                         receipts=[{"item_id": item.id, "lot_no": "L1",
                                    "expiry_on": date(2027, 5, 1), "qty": 10}])
    pay = await session.scalar(select(Payable).where(Payable.source_ref == f"po-{po.id}-r1"))
    assert pay.party_ref == f"supplier:{sup.id}"
