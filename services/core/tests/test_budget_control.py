"""예산 통제 — 약정·예실대비(3-way)·변경이력."""
from datetime import date

from erp.finance.budget_service import BudgetService
from erp.finance.report_service import FinanceReportService
from erp.finance.service import VoucherLineDTO, VoucherService

bs = BudgetService()
rs = FinanceReportService()
vs = VoucherService()


async def test_amend_logs_history(session):
    await bs.amend(session, department_id=1, fiscal_year=2026, account_code="5310",
                   new_amount=1_000_000, reason="초기", user_id=1)
    await bs.amend(session, department_id=1, fiscal_year=2026, account_code="5310",
                   new_amount=1_500_000, reason="증액", user_id=1)
    await session.flush()
    hist = await bs.amendments(session, fiscal_year=2026)
    assert len(hist) == 2
    assert hist[0]["new_amount"] == 1_500_000 and hist[0]["old_amount"] == 1_000_000


async def test_three_way_budget_vs_committed_vs_spent(session):
    await bs.amend(session, department_id=2, fiscal_year=2026, account_code="5310",
                   new_amount=1_000_000, user_id=1)
    # 집행 300k (DEBIT 5310)
    await vs.create(session, source_module="manual", source_ref="bud-spent",
                    voucher_on=date(2026, 5, 1),
                    lines=[VoucherLineDTO("5310", "DEBIT", 300_000, department_id=2),
                           VoucherLineDTO("1110", "CREDIT", 300_000, department_id=2)])
    # 약정 200k
    await bs.encumber(session, department_id=2, fiscal_year=2026, account_code="5310",
                      amount=200_000, source_ref="po-1", user_id=1)
    await session.flush()
    rows = await rs.budget_execution(session, fiscal_year=2026)
    r = next(x for x in rows if x["department_id"] == 2 and x["account_code"] == "5310")
    assert r["spent"] == 300_000 and r["committed"] == 200_000
    assert r["available"] == 500_000 and r["status"] == "NORMAL"


async def test_encumber_idempotent_and_release(session):
    e1 = await bs.encumber(session, department_id=3, fiscal_year=2026, account_code="5310",
                           amount=100_000, source_ref="po-dup", user_id=1)
    e2 = await bs.encumber(session, department_id=3, fiscal_year=2026, account_code="5310",
                           amount=100_000, source_ref="po-dup", user_id=1)
    await session.flush()
    assert e1.id == e2.id   # 멱등
    await bs.release(session, encumbrance_id=e1.id)
    assert e1.status == "RELEASED"
