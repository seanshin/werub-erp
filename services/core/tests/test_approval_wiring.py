"""N단계 결재선 도메인 와이어링 — PO·위수탁계약 (룰 있으면 다단계·없으면 레거시 폴백)."""
from datetime import date

from erp.common.models import ApprovalRule, User
from erp.scm.contract_service import OutsourcingContractService
from erp.scm.models import Item, Supplier
from erp.scm.po_service import PurchaseOrderService

po_svc = PurchaseOrderService()
oc_svc = OutsourcingContractService()


async def _users(session):
    session.add_all([
        User(id=1, email="staff@x.example", role="STAFF", password_hash="x"),
        User(id=2, email="mgr@x.example", role="MANAGER", password_hash="x"),
        User(id=3, email="dir@x.example", role="DIRECTOR", password_hash="x"),
    ])
    await session.flush()


async def _two_step_rule(session, doc_type):
    session.add(ApprovalRule(doc_type=doc_type, min_amount=0,
                             steps=[{"type": "ROLE", "value": "MANAGER"},
                                    {"type": "ROLE", "value": "DIRECTOR"}],
                             active=True, created_by=1))
    await session.flush()


async def test_po_multistep_gates_until_full_chain(session):
    await _users(session)
    await _two_step_rule(session, "PO")
    sup = Supplier(name="대한약품", business_no="1018212345")
    item = Item(code="M001", name="수액", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add_all([sup, item])
    await session.flush()
    po = await po_svc.request(session, supplier_id=sup.id, actor=1,
                              lines=[{"item_id": item.id, "qty": 10, "unit_price": 5000}])
    # 1단계: MANAGER 승인 → 아직 미확정(REQUESTED 유지)
    await po_svc.approve(session, po_id=po.id, actor=2)
    assert po.status == "REQUESTED"
    # 2단계: DIRECTOR 승인 → 발주 확정
    await po_svc.approve(session, po_id=po.id, actor=3)
    assert po.status == "APPROVED" and po.approved_by == 3


async def test_po_no_rule_legacy_single_approve(session):
    await _users(session)
    sup = Supplier(name="대한약품", business_no="1018212345")
    item = Item(code="M002", name="거즈", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add_all([sup, item])
    await session.flush()
    po = await po_svc.request(session, supplier_id=sup.id, actor=1,
                              lines=[{"item_id": item.id, "qty": 10, "unit_price": 5000}])
    await po_svc.approve(session, po_id=po.id, actor=2)   # 룰 없음 → 레거시 단일 승인
    assert po.status == "APPROVED" and po.approved_by == 2


async def test_po_executive_override(session):
    await _users(session)
    await _two_step_rule(session, "PO")
    sup = Supplier(name="대한약품", business_no="1018212345")
    item = Item(code="M003", name="장비", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add_all([sup, item])
    await session.flush()
    po = await po_svc.request(session, supplier_id=sup.id, actor=1,
                              lines=[{"item_id": item.id, "qty": 10, "unit_price": 5000}])
    # 원장(3) 전권 — 1·2단계 건너뛰어 즉시 확정
    await po_svc.approve(session, po_id=po.id, actor=3, override=True)
    assert po.status == "APPROVED" and po.approved_by == 3


async def test_register_forces_approval_when_rule_exists(session):
    # 거버넌스: 결재선 룰이 매칭되면 require_approval=False여도 DRAFT 강제(직접 ACTIVE 우회 차단)
    await _users(session)
    await _two_step_rule(session, "OUTSOURCING_CONTRACT")
    c = await oc_svc.register(session, actor=1, vendor_name="급식", category="MEAL", title="t",
                              monthly_amount=8_000_000, start_on=date(2026, 1, 1),
                              end_on=date(2026, 12, 31))   # require_approval 기본 False
    assert c.status == "DRAFT"      # 룰 존재 → 승인 경로 강제


async def test_register_stays_active_without_rule(session):
    # 룰 없으면 기존 동작(즉시 ACTIVE) 유지 — 무회귀
    c = await oc_svc.register(session, actor=1, vendor_name="세탁", category="LAUNDRY", title="t",
                              monthly_amount=1_000_000, start_on=date(2026, 1, 1),
                              end_on=date(2026, 12, 31))
    assert c.status == "ACTIVE"


async def test_outsourcing_contract_multistep(session):
    await _users(session)
    await _two_step_rule(session, "OUTSOURCING_CONTRACT")
    c = await oc_svc.register(session, actor=1, vendor_name="급식", category="MEAL", title="t",
                              monthly_amount=8_000_000, start_on=date(2026, 1, 1),
                              end_on=date(2026, 12, 31), require_approval=True)
    assert c.status == "DRAFT"
    await oc_svc.submit_for_approval(session, actor=1, contract_id=c.id)
    assert c.status == "PENDING_APPROVAL"
    await oc_svc.approve(session, actor=2, contract_id=c.id)   # 1단계 MANAGER
    assert c.status == "PENDING_APPROVAL"                       # 아직 미확정
    await oc_svc.approve(session, actor=3, contract_id=c.id)   # 2단계 DIRECTOR → 확정
    assert c.status == "APPROVED" and c.approved_by == 3
