"""4대보험 취득·상실 신고 추적 (P1-③) — 자동적재·멱등·기한·처리."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.hr.absence_service import AbsenceService
from erp.hr.employee_service import EmployeeService
from erp.hr.insurance_report_service import InsuranceReportService
from erp.hr.models import Employee

svc = InsuranceReportService()
_absence = AbsenceService()


async def test_hire_creates_acquire_event(session):
    emp = await EmployeeService().create(
        session, actor=1, employee_no="E0300", name="신입사",
        hired_on=date(2026, 6, 1))
    items = await svc.pending(session)
    acq = [i for i in items if i["employee_id"] == emp.id and i["event"] == "ACQUIRE"]
    assert len(acq) == 1
    assert acq[0]["due_on"] == date(2026, 6, 15)            # +14일

    # 멱등 — 동일 (직원, 이벤트, 사유일) 재적재 무시
    assert (await svc.record(session, employee_id=emp.id, event="ACQUIRE",
                             basis_on=date(2026, 6, 1))) is None


async def test_absence_and_resign_events(session):
    emp = Employee(employee_no="E0310", name="휴퇴직", hired_on=date(2025, 1, 1),
                   job_group="STAFF", employment_type="REGULAR", source="ERP")
    session.add(emp)
    await session.flush()

    a = await _absence.start(session, actor=1, employee_id=emp.id, kind="PARENTAL",
                             start_on=date(2026, 7, 1))
    await _absence.end(session, actor=1, absence_id=a.id, return_on=date(2026, 10, 1))
    await _absence.resign(session, actor=1, employee_id=emp.id,
                          resigned_on=date(2026, 12, 31))

    events = {i["event"] for i in await svc.pending(session)
              if i["employee_id"] == emp.id}
    assert {"EXEMPT", "RESUME", "LOSE"} <= events


async def test_exempt_skipped_when_not_exempt(session):
    emp = Employee(employee_no="E0320", name="비예외", hired_on=date(2025, 1, 1),
                   job_group="STAFF", employment_type="REGULAR", source="ERP")
    session.add(emp)
    await session.flush()
    await _absence.start(session, actor=1, employee_id=emp.id, kind="PERSONAL",
                         start_on=date(2026, 7, 1), insurance_exempt=False)
    assert not [i for i in await svc.pending(session)
                if i["employee_id"] == emp.id and i["event"] == "EXEMPT"]


async def test_mark_reported_and_overdue(session):
    emp = Employee(employee_no="E0330", name="기한자", hired_on=date(2025, 1, 1),
                   job_group="STAFF", employment_type="REGULAR", source="ERP")
    session.add(emp)
    await session.flush()
    ev = await svc.record(session, employee_id=emp.id, event="ACQUIRE",
                          basis_on=date(2026, 5, 1))

    items = await svc.pending(session, today=date(2026, 6, 1))
    mine = next(i for i in items if i["id"] == ev.id)
    assert mine["overdue"] is True                          # 기한(5/15) 경과

    await svc.mark_reported(session, actor=1, event_id=ev.id,
                            reported_on=date(2026, 6, 2))
    assert not [i for i in await svc.pending(session) if i["id"] == ev.id]
    reported = await svc.pending(session, status="REPORTED")
    assert any(i["id"] == ev.id for i in reported)
    with pytest.raises(ValidationError):                    # 이중 처리 차단
        await svc.mark_reported(session, actor=1, event_id=ev.id,
                                reported_on=date(2026, 6, 3))
