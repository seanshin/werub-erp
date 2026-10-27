"""휴직/복직·퇴사 (P1-①) — 기간판정·중복차단·발령연동·SoT 가드·일괄생성 제외."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ForbiddenError, ValidationError
from erp.hr.absence_service import AbsenceService
from erp.hr.models import Appointment, Employee

svc = AbsenceService()


def _emp(no: str, name: str, **kw) -> Employee:
    defaults = dict(employee_no=no, name=name, hired_on=date(2025, 1, 1),
                    job_group="STAFF", employment_type="REGULAR", source="ERP")
    defaults.update(kw)
    return Employee(**defaults)


async def test_absence_lifecycle_with_appointments(session):
    emp = _emp("E0100", "한육아")
    session.add(emp)
    await session.flush()

    a = await svc.start(session, actor=1, employee_id=emp.id, kind="PARENTAL",
                        start_on=date(2026, 7, 1), expected_return_on=date(2027, 6, 30))
    assert a.insurance_exempt is True

    # 휴직 중 재휴직 차단
    with pytest.raises(ValidationError):
        await svc.start(session, actor=1, employee_id=emp.id, kind="SICK",
                        start_on=date(2026, 8, 1))

    # 기간 판정 — 휴직 중/이전/복직 후
    assert (await svc.active_absence(session, employee_id=emp.id, on=date(2026, 8, 15))) is not None
    assert (await svc.active_absence(session, employee_id=emp.id, on=date(2026, 6, 30))) is None

    await svc.end(session, actor=1, absence_id=a.id, return_on=date(2027, 3, 2))
    assert (await svc.active_absence(session, employee_id=emp.id, on=date(2027, 3, 2))) is None
    with pytest.raises(ValidationError):                       # 이중 복직 차단
        await svc.end(session, actor=1, absence_id=a.id, return_on=date(2027, 4, 1))

    # 발령 이력 — LEAVE + REINSTATE
    types = (await session.execute(
        select(Appointment.appointment_type)
        .where(Appointment.employee_id == emp.id))).scalars().all()
    assert "LEAVE" in types and "REINSTATE" in types


async def test_on_leave_ids_for_bulk_exclusion(session):
    on_leave = _emp("E0110", "박휴직")
    working = _emp("E0111", "근무중")
    session.add_all([on_leave, working])
    await session.flush()
    await svc.start(session, actor=1, employee_id=on_leave.id, kind="SICK",
                    start_on=date(2026, 6, 20))

    ids = await svc.on_leave_employee_ids(session, on=date(2026, 7, 1))
    assert on_leave.id in ids and working.id not in ids
    assert on_leave.id not in await svc.on_leave_employee_ids(session, on=date(2026, 6, 1))


async def test_resign_sets_date_and_guards(session):
    erp_emp = _emp("E0120", "김퇴사")
    his_emp = _emp("H0001", "이의사", source="HIS")
    session.add_all([erp_emp, his_emp])
    await session.flush()

    emp = await svc.resign(session, actor=1, employee_id=erp_emp.id,
                           resigned_on=date(2026, 6, 30))
    assert emp.resigned_on == date(2026, 6, 30)
    types = (await session.execute(
        select(Appointment.appointment_type)
        .where(Appointment.employee_id == erp_emp.id))).scalars().all()
    assert "RESIGN" in types

    with pytest.raises(ValidationError):                       # 이중 퇴사 차단
        await svc.resign(session, actor=1, employee_id=erp_emp.id,
                         resigned_on=date(2026, 7, 1))
    with pytest.raises(ForbiddenError):                        # SoT — HIS 직원 차단
        await svc.resign(session, actor=1, employee_id=his_emp.id,
                         resigned_on=date(2026, 6, 30))
    # 퇴직자 휴직 차단
    with pytest.raises(ValidationError):
        await svc.start(session, actor=1, employee_id=erp_emp.id, kind="SICK",
                        start_on=date(2026, 7, 1))


async def test_absence_list_dept_self_scope(session):
    """휴직 목록 행 scope(§6.2 P0-3) — dept_id 본부서·employee_id 본인만 필터."""
    e1 = _emp("E0201", "내과직원", department_id=10)
    e2 = _emp("E0202", "외과직원", department_id=20)
    session.add_all([e1, e2])
    await session.flush()
    await svc.start(session, actor=1, employee_id=e1.id, kind="SICK", start_on=date(2026, 7, 1))
    await svc.start(session, actor=1, employee_id=e2.id, kind="SICK", start_on=date(2026, 7, 1))
    # 전체(ALL)
    assert {x["employee_no"] for x in await svc.list(session)} >= {"E0201", "E0202"}
    # 부서(DEPT 10) — 본부서만
    assert {x["employee_no"] for x in await svc.list(session, dept_id=10)} == {"E0201"}
    # 본인(SELF) — 본인만
    assert {x["employee_no"] for x in await svc.list(session, employee_id=e1.id)} == {"E0201"}
