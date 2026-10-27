"""월마감 일괄 — 감가상각·퇴직충당·리스 통합 실행·멱등."""
from datetime import date

from erp.finance.models import Account, FixedAsset
from erp.app.month_end_service import MonthEndService
from erp.hr.models import Employee, PayrollLine, PayrollRecord

svc = MonthEndService()


async def _seed(session):
    session.add_all([Account(code=c, name=c, kind=k) for c, k in [
        ("1710", "ASSET"), ("1810", "ASSET"), ("5210", "EXPENSE"),
        ("2120", "LIABILITY"), ("5320", "EXPENSE")]])
    session.add(FixedAsset(asset_no="A1", name="장비", category="MEDICAL",
                           acquired_on=date(2025, 1, 1), acquisition_cost=12_000_000,
                           useful_life_years=5, accumulated_depreciation=0))
    e = Employee(employee_no="M1", name="직원", hired_on=date(2022, 1, 1))
    session.add(e)
    await session.flush()
    rec = PayrollRecord(employee_id=e.id, yyyymm="2026-05")
    session.add(rec)
    await session.flush()
    session.add(PayrollLine(record_id=rec.id, item_kind="EARNING", item_code="BASE", amount=3_000_000))
    await session.flush()


async def test_month_end_runs_all_and_idempotent(session):
    await _seed(session)
    out = await svc.run(session, yyyymm="2026-05")
    assert out["depreciation_count"] >= 1 and out["retirement_employees"] >= 1
    await session.flush()
    out2 = await svc.run(session, yyyymm="2026-05")   # 재실행 — 멱등(중복 전표 없음)
    assert out2["depreciation_count"] == 0   # 이미 상각된 자산 스킵


async def test_month_end_accrues_outsourcing_contracts(session):
    from erp.scm.contract_service import OutsourcingContractService
    await OutsourcingContractService().register(
        session, actor=1, vendor_name="급식", category="MEAL", title="급식위탁",
        monthly_amount=8_000_000, start_on=date(2026, 1, 1), end_on=date(2026, 12, 31))
    await session.flush()
    out = await svc.run(session, yyyymm="2026-05")
    assert out["contract_accrued"] == 1 and out["contract_skipped"] == 0
    await session.flush()
    out2 = await svc.run(session, yyyymm="2026-05")     # 멱등 — 위탁료 중복 발생 없음
    assert out2["contract_accrued"] == 0 and out2["contract_skipped"] == 1
