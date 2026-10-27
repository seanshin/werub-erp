"""퇴직급여 보험수리적 평가 (K-IFRS 1019) — DBO=명목×((1+g)/(1+d))^n·재측정 OCI·멱등."""
from datetime import date

from erp.finance.models import Account
from erp.hr.models import (
    ActuarialAssumption, Employee, PayrollLine, PayrollRecord,
)
from erp.hr.retirement_service import RetirementService

svc = RetirementService()


async def _seed(session):
    session.add_all([
        Account(code="2120", name="퇴직급여충당부채", kind="LIABILITY"),
        Account(code="5320", name="퇴직급여", kind="EXPENSE"),
        Account(code="3410", name="보험수리적손익", kind="CAPITAL"),
        ActuarialAssumption(discount_rate=0.04, salary_growth=0.03, retirement_age=60,
                            valid_from=date(2025, 1, 1)),
    ])
    # 직원: 2016-06-30 입사(근속 10년), 1981년생(2026 기준 45세, 잔여 15년), DB형
    emp = Employee(employee_no="D1", name="의사", hired_on=date(2016, 6, 30),
                   birth_date=date(1981, 6, 30), department_id=1, job_group="DOCTOR",
                   pension_type="DB")
    session.add(emp)
    await session.flush()
    rec = PayrollRecord(employee_id=emp.id, yyyymm="2026-06")
    session.add(rec)
    await session.flush()
    session.add(PayrollLine(record_id=rec.id, item_kind="EARNING", item_code="BASE",
                            amount=5000000))
    await session.flush()
    return emp


async def test_dbo_preview_discounts(session):
    await _seed(session)
    p = await svc.dbo_preview(session, period="2026-06")
    # 명목 ≈ 500만×(근속일/365) ≈ 5천만, factor=((1.03)/(1.04))^15<1 → DBO<명목
    assert p["dbo"] > 0 and p["nominal_estimate"] > p["dbo"]
    # 재측정(OCI) = DBO − 명목추계 < 0 (할인>임금상승 → 보험수리적이익)
    assert p["remeasurement"] == p["dbo"] - p["nominal_estimate"] < 0


async def test_dbo_post_idempotent(session):
    await _seed(session)
    r1 = await svc.dbo_post(session, period="2026-06")
    assert r1["posted"] and r1["remeasurement"] != 0
    r2 = await svc.dbo_post(session, period="2026-06")
    assert r2["posted"] is False        # 멱등 — 동일 기간 재전기 차단(source_ref 선검사)
