"""급여 법정처리 행 scope (code-analyzer C-1) — STAFF는 본인 정보만 조회."""
from datetime import date

import pytest

from erp.common.models import User
from erp.common.router import CurrentUser
from erp.errors import ForbiddenError
from erp.hr.models import Employee
from erp.hr.router import employee_severance, payroll_insurance, physician_revenue


async def _dept1(session):
    """department_id=1 참조 FK 충족(PG는 강제·sqlite는 무시 — P2-7 PG 레인 대응)."""
    from erp.common.models import Department
    if await session.get(Department, 1) is None:
        session.add(Department(id=1, name="진료부"))
        await session.flush()


async def _staff_for(session, emp_id):
    u = User(email=f"u{emp_id}@x.example", password_hash="x", role="STAFF",
             department_id=1, employee_id=emp_id)
    session.add(u)
    await session.flush()
    return CurrentUser(user_id=u.id, role="STAFF", dept_id=1, license_flag=True)


async def test_staff_self_only_severance(session):
    await _dept1(session)
    a = Employee(employee_no="A1", name="본인", hired_on=date(2020, 1, 1),
                 department_id=1, job_group="NURSE")
    b = Employee(employee_no="B1", name="타인", hired_on=date(2020, 1, 1),
                 department_id=1, job_group="NURSE")
    session.add_all([a, b])
    await session.flush()
    staff = await _staff_for(session, a.id)

    out = await employee_severance(employee_id=a.id, as_of=date(2026, 1, 1),
                                   user=staff, session=session)
    assert "퇴직금" in out                                   # 본인 → 허용
    with pytest.raises(ForbiddenError, match="본인"):
        await employee_severance(employee_id=b.id, as_of=date(2026, 1, 1),
                                 user=staff, session=session)   # 타인 → 차단


async def test_staff_self_only_insurance(session):
    await _dept1(session)
    a = Employee(employee_no="A2", name="본인", hired_on=date(2020, 1, 1),
                 department_id=1, job_group="NURSE")
    b = Employee(employee_no="B2", name="타인", hired_on=date(2020, 1, 1),
                 department_id=1, job_group="NURSE")
    session.add_all([a, b])
    await session.flush()
    staff = await _staff_for(session, a.id)

    await payroll_insurance(yyyymm="2026-05", employee_id=a.id, user=staff, session=session)
    with pytest.raises(ForbiddenError):
        await payroll_insurance(yyyymm="2026-05", employee_id=b.id, user=staff, session=session)


async def test_physician_revenue_idor_blocked(session):
    """심층 재검토 HIGH — physician-revenue: STAFF는 타 의사·전체 실적 조회 불가(IDOR)."""
    await _dept1(session)
    a = Employee(employee_no="A3", name="본인의사", hired_on=date(2020, 1, 1),
                 department_id=1, job_group="DOCTOR")
    b = Employee(employee_no="B3", name="타의사", hired_on=date(2020, 1, 1),
                 department_id=1, job_group="DOCTOR")
    session.add_all([a, b])
    await session.flush()
    staff = await _staff_for(session, a.id)

    out = await physician_revenue(period="2026-06", doctor_id=a.id, user=staff, session=session)
    assert "rows" in out                                     # 본인 → 허용
    with pytest.raises(ForbiddenError, match="본인"):
        await physician_revenue(period="2026-06", doctor_id=b.id, user=staff,
                                session=session)             # 타 의사 → 차단
    with pytest.raises(ForbiddenError, match="관리자"):
        await physician_revenue(period="2026-06", doctor_id=None, user=staff,
                                session=session)             # 전체 덤프 → 차단


async def test_physician_revenue_admin_all(session):
    """관리자(ALL)는 특정/전체 의사 실적 모두 조회 가능."""
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    out = await physician_revenue(period="2026-06", doctor_id=None, user=admin, session=session)
    assert "rows" in out                                     # 전체 집계 허용
