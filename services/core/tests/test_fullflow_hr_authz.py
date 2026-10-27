"""2026-10-09 전체 흐름 검토 G — `_scope()` 반환값을 버리고 SELF/DEPT 범위를 무시하던 HR 엔드포인트.

STAFF(hr.payroll read=SELF)가 타인/전사 징계·근로계약·퇴직충당·교육이수·급여정책을 읽던 결함 회귀.
"""
from datetime import date

from erp.common.models import User
from erp.hr.models import Employee
from tests._http import auth_as


async def _staff_with_employee(session, *, user_id: int, dept_id: int | None = None):
    me = Employee(employee_no=f"AZ-{user_id}", name="본인", hired_on=date(2024, 1, 1),
                  job_group="STAFF", employment_type="REGULAR", source="ERP",
                  department_id=dept_id)
    other = Employee(employee_no=f"AZ-{user_id}-O", name="타인", hired_on=date(2024, 1, 1),
                     job_group="STAFF", employment_type="REGULAR", source="ERP",
                     department_id=(dept_id or 0) + 1000)
    session.add_all([me, other])
    await session.flush()
    return me, other


async def _link(session, user_id: int, employee_id: int) -> None:
    u = await session.get(User, user_id)
    u.employee_id = employee_id
    await session.flush()


async def test_staff_blocked_from_others_and_company_wide_hr_data(session, client):
    h = await auth_as(session, user_id=701, role="STAFF")
    me, other = await _staff_with_employee(session, user_id=701)
    await _link(session, 701, me.id)

    # 직원별 — 본인은 허용, 타인은 403
    for path in ("/api/v1/hr/employment-contracts?employee_id={}",
                 "/api/v1/hr/disciplinary-actions?employee_id={}",
                 "/api/v1/hr/training/completions?employee_id={}"):
        own = await client.get(path.format(me.id), headers=h)
        assert own.status_code == 200, (path, own.text)
        r = await client.get(path.format(other.id), headers=h)
        assert r.status_code == 403, (path, r.status_code, r.text)

    # 전사 목록·집계 — ALL만
    for path in ("/api/v1/hr/disciplinary-actions",
                 "/api/v1/hr/employment-contracts/expiring",
                 "/api/v1/hr/retirement/provisions/2026-06",
                 "/api/v1/hr/retirement/dbo/preview/2026-06",
                 "/api/v1/admin/payroll-settings"):
        r = await client.get(path, headers=h)
        assert r.status_code == 403, (path, r.status_code, r.text)


async def test_admin_still_reads_company_wide(session, client):
    h = await auth_as(session, user_id=702, role="ADMIN")
    for path in ("/api/v1/hr/disciplinary-actions",
                 "/api/v1/hr/employment-contracts/expiring",
                 "/api/v1/admin/payroll-settings"):
        r = await client.get(path, headers=h)
        assert r.status_code == 200, (path, r.text)


async def test_manager_dept_scope_on_training_completions(session, client):
    """hr.employees read=DEPT(MANAGER) — 타 부서 직원 교육이수는 403, 자기 부서는 허용."""
    h = await auth_as(session, user_id=703, role="MANAGER", dept_id=77)
    mine, other = await _staff_with_employee(session, user_id=703, dept_id=77)
    assert (await client.get(f"/api/v1/hr/training/completions?employee_id={mine.id}",
                             headers=h)).status_code == 200
    r = await client.get(f"/api/v1/hr/training/completions?employee_id={other.id}", headers=h)
    assert r.status_code == 403, r.text
