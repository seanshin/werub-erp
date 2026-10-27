"""구매요청(PR→PO)·자재청구(요청→승인→불출) — SoD·상태머신·전환/출고 (0217)."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ForbiddenError, ValidationError
from erp.scm.models import Item, Lot, PurchaseOrder, StockMovement, Supplier
from erp.scm.requisition_service import MaterialRequestService, PurchaseRequisitionService

pr = PurchaseRequisitionService()
mr = MaterialRequestService()
ON = date(2026, 7, 21)


async def _item(session, code="M1"):
    it = Item(code=code, name="수액", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add(it)
    await session.flush()
    return it


# ── 구매요청 ───────────────────────────────────────────────
async def test_purchase_requisition_to_po(session):
    """기안(PENDING)→승인(SoD)→발주 전환(CONVERTED·PO 생성)."""
    it = await _item(session)
    sup = Supplier(name="대한약품", business_no="1018212345")
    session.add(sup)
    await session.flush()
    req = await pr.create(session, requester=1, department_id=3,
                          lines=[{"item_id": it.id, "qty": 100, "est_price": 5000}],
                          reason="수액 재고 소진", on=ON)
    assert req.req_no == "구매요청-2026-0001" and req.status == "PENDING"
    with pytest.raises(ValidationError, match="직무분리"):     # 요청자 본인 승인 차단
        await pr.decide(session, req_id=req.id, approve=True, actor=1)
    await pr.decide(session, req_id=req.id, approve=True, actor=2)
    assert req.status == "APPROVED"
    out = await pr.convert_to_po(session, req_id=req.id, supplier_id=sup.id,
                                 unit_prices={str(it.id): 4800}, actor=2)
    assert req.status == "CONVERTED" and req.po_id == out["po_id"]
    po = await session.get(PurchaseOrder, out["po_id"])
    assert po.status == "REQUESTED" and po.department_id == 3


async def test_po_reject_reopens_converted_requisition(session):
    """v1.203 — 전환 PO 반려 시 PR을 APPROVED로 되돌림(CONVERTED 데드엔드·수요 무음소실 방지)."""
    from erp.scm.po_service import PurchaseOrderService
    it = await _item(session)
    sup = Supplier(name="공급사A", business_no="2018212345")
    session.add(sup)
    await session.flush()
    req = await pr.create(session, requester=1, department_id=3,
                          lines=[{"item_id": it.id, "qty": 5, "est_price": 1000}],
                          reason="테스트", on=ON)
    await pr.decide(session, req_id=req.id, approve=True, actor=2)
    out = await pr.convert_to_po(session, req_id=req.id, supplier_id=sup.id,
                                 unit_prices={}, actor=2)
    assert req.status == "CONVERTED"
    await PurchaseOrderService().reject(session, po_id=out["po_id"], actor=2, reason="예산")
    assert req.status == "APPROVED" and req.po_id is None    # 재전환 가능


async def test_requisition_detail_lines(session):
    """상세 조회 — 헤더+라인(품목명·코드 조인). FE 상세/발주전환 소스."""
    it = await _item(session)
    req = await pr.create(session, requester=1, department_id=3,
                          lines=[{"item_id": it.id, "qty": 10, "est_price": 500, "note": "긴급"}],
                          reason="소모품", on=ON)
    d = await pr.get_detail(session, req.id)
    assert d["req_no"] == req.req_no and d["status"] == "PENDING"
    assert d["lines"] == [{"item_id": it.id, "item_code": "M1", "item_name": "수액",
                           "qty": 10.0, "est_price": 500.0, "note": "긴급"}]
    with pytest.raises(ValidationError):
        await pr.get_detail(session, 99999)


async def test_convert_requires_approved(session):
    it = await _item(session)
    sup = Supplier(name="공급사", business_no="1")
    session.add(sup)
    await session.flush()
    req = await pr.create(session, requester=1, department_id=3,
                          lines=[{"item_id": it.id, "qty": 10, "est_price": 100}], on=ON)
    with pytest.raises(ValidationError, match="승인된 요청만"):    # PENDING 상태 전환 불가
        await pr.convert_to_po(session, req_id=req.id, supplier_id=sup.id,
                               unit_prices={str(it.id): 100}, actor=2)


async def test_requisition_bad_item_and_qty(session):
    with pytest.raises(ValidationError, match="존재하지 않는 품목"):
        await pr.create(session, requester=1, department_id=3,
                        lines=[{"item_id": 999, "qty": 1}], on=ON)
    it = await _item(session)
    with pytest.raises(ValidationError, match="0보다 커야"):
        await pr.create(session, requester=1, department_id=3,
                        lines=[{"item_id": it.id, "qty": 0}], on=ON)


async def test_requisition_cancel_owner_only(session):
    it = await _item(session)
    req = await pr.create(session, requester=1, department_id=3,
                          lines=[{"item_id": it.id, "qty": 5, "est_price": 100}], on=ON)
    with pytest.raises(ForbiddenError):
        await pr.cancel(session, req_id=req.id, requester=9)
    await pr.cancel(session, req_id=req.id, requester=1)
    assert req.status == "CANCELED"


# ── 자재청구 ───────────────────────────────────────────────
async def _stocked_item(session):
    from erp.scm.stock_service import StockService
    it = await _item(session, code="M2")
    await StockService().receive(session, item_id=it.id, lot_no="L1",
                                 expiry_on=date(2027, 5, 1), qty=100, moved_on=ON, actor=1,
                                 unit_cost=5000)
    return it


async def test_material_request_to_issue(session):
    """청구(REQUESTED)→승인(SoD)→FIFO 출고(ISSUED·재고 차감)."""
    it = await _stocked_item(session)
    req = await mr.create(session, requester=1, department_id=3, item_id=it.id, qty=30,
                          reason="병동 사용", on=ON)
    assert req.req_no == "자재청구-2026-0001" and req.status == "REQUESTED"
    with pytest.raises(ValidationError, match="직무분리"):
        await mr.decide(session, req_id=req.id, approve=True, actor=1)
    await mr.decide(session, req_id=req.id, approve=True, actor=2)
    out = await mr.issue(session, req_id=req.id, actor=2, moved_on=ON)
    assert out["status"] == "ISSUED"
    lot = await session.scalar(select(Lot).where(Lot.item_id == it.id))
    assert float(lot.remaining_qty) == 70                      # 100 - 30 출고
    mv = await session.scalar(select(StockMovement).where(
        StockMovement.item_id == it.id, StockMovement.movement_type == "OUT"))
    assert float(mv.qty) == 30


async def test_material_issue_requires_approval(session):
    """승인 전 출고 차단(무승인 즉시OUT 갭 보완 핵심)."""
    it = await _stocked_item(session)
    req = await mr.create(session, requester=1, department_id=3, item_id=it.id, qty=10, on=ON)
    with pytest.raises(ValidationError, match="승인된 청구만"):
        await mr.issue(session, req_id=req.id, actor=2, moved_on=ON)


async def test_material_reject_then_no_issue(session):
    it = await _stocked_item(session)
    req = await mr.create(session, requester=1, department_id=3, item_id=it.id, qty=10, on=ON)
    await mr.decide(session, req_id=req.id, approve=False, actor=2, reason="재고 확인 필요")
    assert req.status == "REJECTED"
    with pytest.raises(ValidationError):
        await mr.issue(session, req_id=req.id, actor=2, moved_on=ON)
