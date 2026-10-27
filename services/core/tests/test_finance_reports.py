"""예산 집행률 (FR-09)·월별 정산·발주 워크플로우 (FR-14)."""
from datetime import date, timedelta

import pytest

from erp.errors import ValidationError
from erp.finance.models import Budget
from erp.finance.report_service import FinanceReportService
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.scm.models import Item, Supplier
from erp.scm.po_service import PurchaseOrderService
from erp.scm.stock_service import StockService

D = date(2026, 6, 7)


async def test_budget_execution_rate(session):
    vouchers, reports = VoucherService(), FinanceReportService()
    session.add(Budget(department_id=10, fiscal_year=2026, account_code="5910",
                       amount=1_000_000))
    await vouchers.create(session, source_module="manual", source_ref="b-1",
                          voucher_on=D,
                          lines=[VoucherLineDTO("5910", "DEBIT", 250_000, department_id=10),
                                 VoucherLineDTO("1110", "CREDIT", 250_000)])
    rows = await reports.budget_execution(session, fiscal_year=2026)
    assert rows[0]["spent"] == 250_000 and rows[0]["rate"] == 25.0


async def test_budget_execution_dept_scope(session):
    """부서장 scope — 타 부서 예산 비노출 (집계 명시 WHERE, design §6.2)."""
    reports = FinanceReportService()
    session.add(Budget(department_id=10, fiscal_year=2026, account_code="5910", amount=100))
    session.add(Budget(department_id=20, fiscal_year=2026, account_code="5910", amount=200))
    rows = await reports.budget_execution(session, fiscal_year=2026, scope_dept_id=10)
    assert len(rows) == 1 and rows[0]["department_id"] == 10


async def test_monthly_summary_reversal_ties_to_trial_balance(session):
    """월별 정산 — 역분개 원(REVERSED)+반전(POSTED)을 각 월에 그대로 반영(시산표와 정합).

    심층 재검토: POSTED만 집계하면 원 전표 월(2026-06)에서 REVERSED가 탈락해 시산표와 불일치.
    역분개는 개방기간(오늘)으로 전기되므로, 원 전표는 제 월에 남고 반전은 반전 월에 잡혀야 각 월이
    시산표와 tie-out 된다(POSTED+REVERSED)."""
    vouchers, reports = VoucherService(), FinanceReportService()
    v = await vouchers.create(session, source_module="manual", source_ref="m-1",
                              voucher_on=D,
                              lines=[VoucherLineDTO("1110", "DEBIT", 100_000),
                                     VoucherLineDTO("4110", "CREDIT", 100_000)])
    reversal = await vouchers.reverse(session, voucher_id=v.id, reason="취소")
    # 반전 월 — 반대 분개(1110 CREDIT·4110 DEBIT) 반영
    rev_summary = await reports.monthly_summary(
        session, yyyymm=reversal.voucher_on.strftime("%Y-%m"))
    assert rev_summary["accounts"]["1110"]["credit"] == 100_000.0
    assert rev_summary["accounts"]["4110"]["debit"] == 100_000.0
    if reversal.voucher_on.strftime("%Y-%m") != "2026-06":
        # 원 전표 월 — REVERSED 원 전표가 그대로 집계(시산표와 정합·POSTED만이면 탈락하던 결함)
        june = await reports.monthly_summary(session, yyyymm="2026-06")
        assert june["accounts"]["1110"]["debit"] == 100_000.0
        assert june["accounts"]["4110"]["credit"] == 100_000.0


async def test_po_workflow_request_approve_receive(session):
    """E2E ③ 단위 버전 — 발주→승인→입고→재고 증가."""
    po_svc, stock = PurchaseOrderService(), StockService()
    supplier = Supplier(name="한국약품", business_no="123-45-67890")
    item = Item(code="MED-010", name="수액", category="DRUG")
    session.add_all([supplier, item])
    await session.flush()

    po = await po_svc.request(session, supplier_id=supplier.id, actor=1,
                              lines=[{"item_id": item.id, "qty": 100, "unit_price": 1500}])
    assert po.status == "REQUESTED"

    with pytest.raises(ValidationError):  # 미승인 입고 차단
        await po_svc.receive(session, po_id=po.id, actor=1, received_on=D,
                             receipts=[{"item_id": item.id, "lot_no": "L-1",
                                        "expiry_on": D + timedelta(days=365), "qty": 100}])

    await po_svc.approve(session, po_id=po.id, actor=2)
    await po_svc.receive(session, po_id=po.id, actor=1, received_on=D,
                         receipts=[{"item_id": item.id, "lot_no": "L-1",
                                    "expiry_on": D + timedelta(days=365), "qty": 100}])
    assert po.status == "RECEIVED"
    assert await stock.current_stock(session, item_id=item.id) == 100.0
