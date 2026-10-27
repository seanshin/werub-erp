"""HR 잔여 데모 — 발령(appointments)·휴직(leave_absences)·법정교육/검진(compliance_records). 멱등·TEST-."""
import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, "/app/src")

from sqlalchemy import select  # noqa: E402

from erp.db import session_factory  # noqa: E402

_SYS = 1


async def sec_appointments(session):
    from erp.hr.employee_service import EmployeeService
    from erp.hr.models import Appointment, Employee
    svc = EmployeeService()
    # ERP 소속(비-HIS) 직원만 발령 가능(HIS SoT 가드). 순환보직 데모.
    emps = (await session.scalars(select(Employee).where(
        Employee.resigned_on.is_(None),
        (Employee.source.is_(None)) | (Employee.source != "HIS")).order_by(Employee.id).limit(8))).all()
    made = 0
    for i, e in enumerate(emps):
        # 이미 TEST 발령 있으면 skip(멱등)
        if await session.scalar(select(Appointment).where(
                Appointment.employee_id == e.id,
                Appointment.note.like("TEST-%"))) is not None:
            continue
        target_dept = 2 if (e.department_id or 0) != 2 else 3
        try:
            await svc.transfer(session, actor=_SYS, employee_id=e.id, to_department_id=target_dept,
                               to_position="순환보직", effective_on=date(2026, 6, 1),
                               note="TEST-정기 순환발령")
            made += 1
        except Exception:  # noqa: BLE001 — 개별 SoT/상태 이슈 skip
            pass
    return f"발령(TRANSFER) {made}건"


async def sec_absences(session):
    from erp.hr.absence_service import AbsenceService
    from erp.hr.models import Employee, LeaveAbsence
    svc = AbsenceService()
    # 부재 없는 직원 일부에 휴직 부여(다양한 사유)
    kinds = ["PARENTAL", "SICK", "MATERNITY", "PERSONAL"]
    emps = (await session.scalars(select(Employee).where(
        Employee.resigned_on.is_(None), Employee.id >= 20).order_by(Employee.id).limit(6))).all()
    made = 0
    for i, e in enumerate(emps):
        if await session.scalar(select(LeaveAbsence).where(
                LeaveAbsence.employee_id == e.id)) is not None:
            continue
        kind = kinds[i % len(kinds)]
        try:
            await svc.start(session, actor=_SYS, employee_id=e.id, kind=kind,
                            start_on=date(2026, 5, 1),
                            expected_return_on=date(2026, 10, 31), note="TEST-휴직")
            made += 1
        except Exception:  # noqa: BLE001
            pass
    return f"휴직(leave_absences) {made}건(LEAVE 발령 동반)"


async def sec_compliance(session):
    from erp.hr.compliance_service import ComplianceService
    from erp.hr.models import Employee
    svc = ComplianceService()
    cats = [("SEXUAL_HARASSMENT", date(2026, 3, 10)), ("PRIVACY", date(2026, 3, 10)),
            ("SAFETY", date(2026, 3, 17)), ("CHECKUP_GENERAL", date(2026, 4, 20))]
    emps = (await session.scalars(select(Employee).where(
        Employee.resigned_on.is_(None)).order_by(Employee.id).limit(50))).all()
    made = 0
    for e in emps:
        for cat, on in cats:
            r = await svc.record(session, actor=_SYS, employee_id=e.id, category=cat,
                                 done_on=on, note="TEST-법정이수")
            made += 1
    # 방사선사 특수건강진단
    techs = (await session.scalars(select(Employee).where(
        Employee.job_category == "RADIOTECH", Employee.resigned_on.is_(None)).limit(5))).all()
    for e in techs:
        await svc.record(session, actor=_SYS, employee_id=e.id, category="CHECKUP_SPECIAL",
                         done_on=date(2026, 4, 25), note="TEST-특수검진")
        made += 1
    return f"법정교육/검진(compliance_records) {made}건(멱등 upsert)"


SECTIONS = [sec_appointments, sec_absences, sec_compliance]


async def main(commit: bool) -> None:
    async with session_factory()() as session:
        for fn in SECTIONS:
            try:
                async with session.begin_nested():
                    msg = await fn(session)
                print(f"  [OK] {fn.__name__}: {msg}")
            except Exception as e:  # noqa: BLE001
                print(f"  [SKIP/ERR] {fn.__name__}: {type(e).__name__}: {e}")
        if commit:
            await session.commit()
            print("== COMMIT 완료 ==")
        else:
            print("== DRY-RUN(미커밋) ==")


if __name__ == "__main__":
    asyncio.run(main("--commit" in sys.argv))
