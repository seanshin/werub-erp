"""갭 감사 보완 검증(v1.185) — H1 연차잔량·M4 자기승인·M6 음수일·H2 부서경계·M3 요청자기승인·
F1/F2 WRITTEN_OFF 제외·F3 반품 환입 미수금."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ForbiddenError, ValidationError

D = date(2026, 7, 21)


# ── H1/M4/M6 연차 ──────────────────────────────────────────
async def _emp(session, dept=10, hired=date(2024, 1, 1), no="N1"):
    from erp.hr.models import Employee
    e = Employee(employee_no=no, name="김간호", hired_on=hired, department_id=dept,
                 job_group="NURSE")
    session.add(e)
    await session.flush()
    return e


async def test_h1_over_approval_blocked_on_decide(session):
    """다중 신청 개별통과 후 승인 시 잔량 재검증 — 초과 승인 차단."""
    from erp.hr.leave_service import LeaveService
    svc = LeaveService()
    emp = await _emp(session)
    await svc.grant_annual(session, employee_id=emp.id, year=2026)   # 15일
    a = await svc.request(session, employee_id=emp.id, leave_type="ANNUAL",
                          start_on=date(2026, 8, 1), end_on=date(2026, 8, 15), days=15)
    b = await svc.request(session, employee_id=emp.id, leave_type="ANNUAL",
                          start_on=date(2026, 9, 1), end_on=date(2026, 9, 15), days=15)
    await svc.decide(session, request_id=a.id, approve=True, decider_user_id=99,
                     decider_dept_id=10, decider_scope_dept_only=True)
    with pytest.raises(ValidationError, match="잔여 연차 초과"):
        await svc.decide(session, request_id=b.id, approve=True, decider_user_id=99,
                         decider_dept_id=10, decider_scope_dept_only=True)


async def test_m4_self_approval_blocked(session):
    """결재자가 신청 당사자면 자기결재 차단."""
    from erp.common.models import User
    from erp.hr.leave_service import LeaveService
    svc = LeaveService()
    emp = await _emp(session)
    session.add(User(id=500, email="a@x.example", role="MANAGER", employee_id=emp.id,
                     password_hash="x", token_version=0, is_active=True))
    await svc.grant_annual(session, employee_id=emp.id, year=2026)
    req = await svc.request(session, employee_id=emp.id, leave_type="ANNUAL",
                            start_on=date(2026, 8, 1), end_on=date(2026, 8, 1), days=1)
    with pytest.raises(ForbiddenError, match="본인 휴가"):
        await svc.decide(session, request_id=req.id, approve=True, decider_user_id=500,
                         decider_dept_id=10, decider_scope_dept_only=False)


async def test_m6_nonpositive_days_rejected(session):
    from erp.hr.leave_service import LeaveService
    svc = LeaveService()
    emp = await _emp(session)
    await svc.grant_annual(session, employee_id=emp.id, year=2026)
    with pytest.raises(ValidationError, match="0보다 커야"):
        await svc.request(session, employee_id=emp.id, leave_type="ANNUAL",
                          start_on=date(2026, 8, 1), end_on=date(2026, 8, 1), days=-5)


# ── H2 구매요청/자재청구 부서 경계 ─────────────────────────
async def test_h2_requisition_dept_enforced(session):
    from erp.scm.models import Item
    from erp.scm.requisition_service import PurchaseRequisitionService
    svc = PurchaseRequisitionService()
    it = Item(code="M1", name="수액", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add(it)
    await session.flush()
    req = await svc.create(session, requester=1, department_id=3,
                           lines=[{"item_id": it.id, "qty": 10, "est_price": 100}], on=D)
    # 타 부서(dept 7) 부서장 승인 차단
    with pytest.raises(ForbiddenError, match="본인 부서"):
        await svc.decide(session, req_id=req.id, approve=True, actor=2,
                         dept_only=True, approver_dept=7)
    # 본부서(dept 3) 승인 통과
    await svc.decide(session, req_id=req.id, approve=True, actor=2,
                     dept_only=True, approver_dept=3)
    assert req.status == "APPROVED"


# ── M3 결재선 요청 자기승인 ────────────────────────────────
async def test_m3_line_request_self_review_blocked(session):
    from erp.common.approval_line_service import ApprovalLineService
    svc = ApprovalLineService()
    req = await svc.submit_request(session, requested_by=5, request_type="NEW", reason="필요",
                                   proposed_name="선", proposed_steps=[{"type": "ROLE",
                                   "value": "DIRECTOR"}], on=D)
    with pytest.raises(ForbiddenError, match="요청자 본인"):
        await svc.review_request(session, request_id=req.id, approve=True, reviewer=5)
    out = await svc.review_request(session, request_id=req.id, approve=True, reviewer=1)
    assert out["status"] == "APPROVED"


# ── F1/F2 WRITTEN_OFF 제외 ─────────────────────────────────
async def _written_off_ar(session):
    from erp.finance.models import Receivable
    r = Receivable(party="C-1", amount=100_000, collected_amount=0, billed_on=date(2026, 5, 1),
                   due_on=date(2026, 5, 31), status="WRITTEN_OFF", source_module="patient",
                   source_ref="patient-ar:C-1:2026-05")
    session.add(r)
    await session.flush()


async def test_f2_aging_by_party_excludes_written_off(session):
    from erp.finance.settlement_service import SettlementService
    await _written_off_ar(session)
    out = await SettlementService().aging_by_party(session, as_of=D, kind="AR")
    assert out["total"] == 0 and out["parties"] == []


async def test_f1_cash_forecast_excludes_written_off(session):
    from erp.finance.settlement_service import SettlementService
    await _written_off_ar(session)
    out = await SettlementService().cash_forecast(session, as_of=D, months=3)
    assert all(p["inflow"] == 0 for p in out["projection"])   # 상각 채권 유입 미포함


# ── F3 반품 환입 미수금 ────────────────────────────────────
async def test_f3_return_after_paid_books_refund_receivable(session):
    """이미 지급된 PO 반품 → DR 미수금(1130)/CR 재고(1150)·공급사 환입 미수금 레코드."""
    from erp.finance.models import Account, Payable, Receivable, Voucher, VoucherLine
    from erp.scm.models import Item, Lot, Supplier
    from erp.scm.po_service import PurchaseOrderService
    from erp.scm.return_service import PurchaseReturnService
    session.add_all([Account(code=c, name=c, kind="ASSET") for c in ("1110", "1130", "1150")]
                    + [Account(code="2110", name="미지급", kind="LIABILITY")])
    sup = Supplier(name="대한약품", business_no="1")
    item = Item(code="M1", name="수액", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add_all([sup, item])
    await session.flush()
    po = await PurchaseOrderService().request(session, supplier_id=sup.id, actor=1,
                                              lines=[{"item_id": item.id, "qty": 10,
                                                      "unit_price": 5000}])
    await PurchaseOrderService().approve(session, po_id=po.id, actor=2)
    await PurchaseOrderService().receive(session, po_id=po.id, actor=2, received_on=D,
                                         receipts=[{"item_id": item.id, "lot_no": "L1",
                                                    "expiry_on": date(2027, 5, 1), "qty": 10}])
    pay = await session.scalar(select(Payable).where(Payable.source_ref == f"po-{po.id}-r1"))
    await SettlementService_pay(session, pay)   # 전액 지급
    lot = await session.scalar(select(Lot).where(Lot.item_id == item.id))
    ret = await PurchaseReturnService().create_return(
        session, po_id=po.id, lot_id=lot.id, qty=2, reason="DAMAGE", returned_on=D, actor=2)
    # 반품 15,000... qty2×5000=10,000. 이미 지급이라 전액 환입(미수금 1130)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == f"po-return-{ret.id}"))
    got = {(x.account_code, x.side, float(x.amount)) for x in (await session.scalars(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).all()}
    assert got == {("1130", "DEBIT", 10_000), ("1150", "CREDIT", 10_000)}
    refund = await session.scalar(select(Receivable).where(
        Receivable.source_ref == f"po-return-refund-{ret.id}"))
    assert refund is not None and float(refund.amount) == 10_000


async def SettlementService_pay(session, pay):
    from erp.finance.settlement_service import SettlementService
    await SettlementService().pay(session, payable_id=pay.id, amount=float(pay.amount),
                                  on=date(2026, 5, 2))
