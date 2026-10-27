"""성능 튜닝 보완(v1.186) 결과 동치 검증 — sargable 치환·overdue SQL 필터·N+1 일괄이 결과 불변."""
from datetime import date

from sqlalchemy import select

from erp.finance.models import Account
from erp.finance.report_service import FinanceReportService
from erp.finance.service import VoucherLineDTO, VoucherService

rpt = FinanceReportService()
vs = VoucherService()


async def _seed_gl(session):
    session.add_all([Account(code="5310", name="급여", kind="EXPENSE"),
                     Account(code="1110", name="현금", kind="ASSET"),
                     Account(code="4110", name="수익", kind="REVENUE")])
    await session.flush()


async def test_budget_execution_year_range_equiv(session):
    """budget_execution 연도 필터 sargable 치환 — 해당 연도만 집계(경계 정확)."""
    from erp.finance.models import Budget
    await _seed_gl(session)
    session.add(Budget(department_id=1, fiscal_year=2026, account_code="5310", amount=1_000_000))
    await session.flush()
    # 2026-01-01(포함)·2026-12-31(포함)·2027-01-01(제외) 경계
    for d, amt in [(date(2026, 1, 1), 100_000), (date(2026, 12, 31), 200_000),
                   (date(2027, 1, 1), 999_999), (date(2025, 12, 31), 888_888)]:
        await vs.create(session, source_module="hr", source_ref=f"b-{d}", voucher_on=d,
                        lines=[VoucherLineDTO("5310", "DEBIT", amt, department_id=1),
                               VoucherLineDTO("1110", "CREDIT", amt)])
    rows = await rpt.budget_execution(session, fiscal_year=2026)
    r = next(x for x in rows if x["account_code"] == "5310")
    assert r["spent"] == 300_000    # 2026분만(1/1+12/31), 2027·2025 제외


async def test_monthly_summary_month_range_equiv(session):
    """monthly_summary 월 범위 sargable — 해당 월만."""
    await _seed_gl(session)
    for d, amt in [(date(2026, 7, 1), 50_000), (date(2026, 7, 31), 30_000),
                   (date(2026, 8, 1), 999)]:
        await vs.create(session, source_module="x", source_ref=f"m-{d}", voucher_on=d,
                        lines=[VoucherLineDTO("1110", "DEBIT", amt),
                               VoucherLineDTO("4110", "CREDIT", amt)])
    out = await rpt.monthly_summary(session, yyyymm="2026-07")
    assert out["accounts"]["1110"]["debit"] == 80_000   # 7월만(8/1 제외)


async def test_overdue_list_sql_days_filter(session):
    """overdue_list — 경과일 SQL 필터(due_on ≤ as_of−min_days)로 정확 선별."""
    from erp.finance.models import Receivable
    from erp.patient.collection_service import PatientCollectionService
    svc = PatientCollectionService()
    session.add_all([
        Receivable(party="C-1", amount=10_000, collected_amount=0, billed_on=date(2026, 6, 1),
                   due_on=date(2026, 6, 30), status="OUTSTANDING", source_module="patient",
                   source_ref="patient-ar:C-1:2026-06"),   # 21일 경과(>=1)
        Receivable(party="C-2", amount=20_000, collected_amount=0, billed_on=date(2026, 7, 1),
                   due_on=date(2026, 7, 25), status="OUTSTANDING", source_module="patient",
                   source_ref="patient-ar:C-2:2026-07"),   # 미도래(as_of 7/21)
    ])
    await session.flush()
    out = await svc.overdue_list(session, as_of=date(2026, 7, 21), min_days=1)
    assert out["count"] == 1 and out["items"][0]["chart_no"] == "C-1"


async def test_settle_card_month_eligible_batch_equiv(session):
    """settle_card_month N+1→일괄 — 승인 청구 귀속분만 정산(결과 동일)."""
    from erp.finance.expense_service import ExpenseService
    session.add_all([Account(code=c, name=c, kind="EXPENSE") for c in ("5340",)]
                    + [Account(code=c, name=c, kind="ASSET") for c in ("1110", "1132")]
                    + [Account(code=c, name=c, kind="LIABILITY") for c in ("2110", "2112")])
    await session.flush()
    esvc = ExpenseService()
    card = await esvc.register_card(session, alias="법인1", card_last4="1234")
    await esvc.import_transactions(session, card_id=card.id, rows=[
        {"tx_id": "T1", "tx_on": date(2026, 7, 5), "amount": 40_000},
        {"tx_id": "T2", "tx_on": date(2026, 7, 6), "amount": 60_000}])
    from erp.finance.models import CardTransaction
    tx1 = await session.scalar(select(CardTransaction).where(CardTransaction.tx_id == "T1"))
    c = await esvc.create_claim(session, employee_id=1, department_id=3, title="카드",
                                claim_on=date(2026, 7, 6), created_by=100,
                                lines=[{"expense_on": date(2026, 7, 5), "account_code": "5340",
                                        "description": "주유", "amount": 40_000,
                                        "card_tx_id": tx1.id}])
    await esvc.submit(session, claim_id=c.id, actor=100)
    await esvc.approve(session, claim_id=c.id, actor=200)
    st = await esvc.settle_card_month(session, card_id=card.id, yyyymm="2026-07",
                                      paid_on=date(2026, 8, 5), actor=1)
    assert st["settled"] == 40_000 and st["count"] == 1   # T2(미청구) 제외
