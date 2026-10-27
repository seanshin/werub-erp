"""퇴직 정산(오프보딩, P2-①) — 연차수당·퇴직금·충당상계·전표·멱등."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.models import VoucherLine
from erp.hr.absence_service import AbsenceService
from erp.hr.compensation_service import CompensationService
from erp.hr.leave_service import LeaveService
from erp.hr.models import Employee, RetirementProvision
from erp.hr.offboarding_service import OffboardingService

svc = OffboardingService()


async def _resigned_emp(session, no="E0400", base_pay=2_090_000,
                        hired=date(2024, 1, 1), resigned=date(2026, 6, 30)) -> Employee:
    emp = Employee(employee_no=no, name="정퇴직", hired_on=hired, job_group="STAFF",
                   employment_type="REGULAR", source="ERP")
    session.add(emp)
    await session.flush()
    await CompensationService().upsert(session, actor=1, employee_id=emp.id,
                                       base_pay=base_pay)
    await AbsenceService().resign(session, actor=1, employee_id=emp.id,
                                  resigned_on=resigned)
    return emp


async def test_preview_requires_resignation(session):
    emp = Employee(employee_no="E0410", name="재직중", hired_on=date(2025, 1, 1),
                   job_group="STAFF", employment_type="REGULAR", source="ERP")
    session.add(emp)
    await session.flush()
    with pytest.raises(ValidationError, match="퇴사 처리"):
        await svc.preview(session, employee_id=emp.id)


async def test_settle_full_flow_with_voucher(session):
    emp = await _resigned_emp(session)
    # 연차 10일 부여(사용 0) + 충당 누적 3,000,000
    await LeaveService().grant_annual(session, employee_id=emp.id, year=2026)
    session.add(RetirementProvision(employee_id=emp.id, period="2026-05",
                                    avg_wage=2_090_000, estimate=5_000_000,
                                    increment=0, cumulative=3_000_000))
    await session.flush()

    pv = await svc.preview(session, employee_id=emp.id)
    assert pv["already_settled"] is False
    assert pv["leave_remaining_days"] > 0
    assert pv["severance"]["지급대상"] is True            # 근속 2.5년
    # 통상일급 = 2,090,000/209*8 = 80,000
    assert pv["leave_allowance"] == round(pv["leave_remaining_days"] * 80_000)

    out = await svc.settle(session, actor=1, employee_id=emp.id)
    assert out["voucher_id"] is not None
    assert out["provision_offset"] == 3_000_000           # 충당 상계

    # 전표 차대 검증: 차변(충당상계+비용+연차수당) = 대변(미지급금 총액)
    lines = (await session.scalars(select(VoucherLine).where(
        VoucherLine.voucher_id == out["voucher_id"]))).all()
    debit = sum(float(li.amount) for li in lines if li.side == "DEBIT")
    credit = sum(float(li.amount) for li in lines if li.side == "CREDIT")
    assert debit == credit == float(pv["severance"]["퇴직금"]) + pv["leave_allowance"]

    # 멱등 — 재정산 차단
    with pytest.raises(ValidationError, match="이미 정산"):
        await svc.settle(session, actor=1, employee_id=emp.id)
    assert (await svc.preview(session, employee_id=emp.id))["already_settled"] is True


async def test_settle_under_one_year_no_severance(session):
    emp = await _resigned_emp(session, no="E0420", hired=date(2026, 1, 2),
                              resigned=date(2026, 6, 30))
    out = await svc.settle(session, actor=1, employee_id=emp.id)
    assert out["severance"]["지급대상"] is False
    assert float(out["severance"]["퇴직금"]) == 0
