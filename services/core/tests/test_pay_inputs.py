"""근태·듀티 → 급여 자동산정 (P1-②) — 야간/당직/연장 해소 + OT 가산 라인."""
from datetime import date

from erp.hr import payroll_rules
from erp.hr.models import AttendanceSummary, DutyAssignment, DutySchedule, Employee
from erp.hr.pay_inputs import resolve_monthly_inputs

YM = "2026-07"


def _emp(no: str, name: str, **kw) -> Employee:
    defaults = dict(employee_no=no, name=name, hired_on=date(2025, 1, 1),
                    job_group="STAFF", employment_type="REGULAR", source="ERP")
    defaults.update(kw)
    return Employee(**defaults)


async def test_nurse_night_and_ot_from_attendance(session):
    nurse = _emp("E0200", "윤간호", job_group="NURSE", job_category="NURSE")
    session.add(nurse)
    await session.flush()
    # 20일 근무 · 야간 4회 · 총 170h(=160h 소정 + 10h 연장)
    session.add(AttendanceSummary(employee_id=nurse.id, yyyymm=YM, work_days=20,
                                  night_shift_count=4, total_work_minutes=170 * 60))
    await session.flush()

    auto = await resolve_monthly_inputs(session, nurse, YM)
    assert auto["night_hours"] == 32.0          # 4회 × 8h
    assert auto["ot_hours"] == 10.0
    assert auto["work_days"] == 20

    # "202607" 표기도 동일 해소
    assert (await resolve_monthly_inputs(session, nurse, "202607"))["ot_hours"] == 10.0


async def test_doctor_duty_count_from_roster(session):
    doc = _emp("E0210", "강의사", job_group="DOCTOR", job_category="DOCTOR",
               staff_class="MEDICAL", department_id=1)
    session.add(doc)
    await session.flush()
    sched = DutySchedule(department_id=1, yyyymm=YM)
    session.add(sched)
    await session.flush()
    for d, shift in ((1, "N"), (8, "N"), (15, "D"), (22, "N")):
        session.add(DutyAssignment(schedule_id=sched.id, employee_id=doc.id,
                                   duty_on=date(2026, 7, d), shift=shift))
    await session.flush()

    auto = await resolve_monthly_inputs(session, doc, YM)
    assert auto["duty_count"] == 3              # N 배정만 당직
    assert auto["night_hours"] == 0.0           # 간호사 전용 항목


async def test_no_attendance_is_all_zero(session):
    emp = _emp("E0220", "무근태")
    session.add(emp)
    await session.flush()
    auto = await resolve_monthly_inputs(session, emp, YM)
    assert auto == {"work_days": 0, "night_hours": 0.0, "duty_count": 0,
                    "ot_hours": 0.0, "holiday_hours": 0.0, "holiday_ot_hours": 0.0,
                    "unpaid_days": 0.0, "work_hours": 0.0}


def test_build_payroll_ot_line():
    r = payroll_rules.build_payroll(employment_type="REGULAR", base_pay=2_090_000,
                                    ot_hours=10)
    ot = next(line for line in r["lines"] if line["item_code"] == "OT")
    assert ot["amount"] == 150_000              # 시급 10,000 × 10h × 1.5

    # 일용직은 OT 라인 제외(일당 정산)
    r2 = payroll_rules.build_payroll(employment_type="DAILY", daily_wage=150_000,
                                     work_days=10, ot_hours=5)
    assert not any(line["item_code"] == "OT" for line in r2["lines"])


async def test_unpaid_leave_daily_deduction(session):
    """무급휴가 일할 공제 (HIS 합의 2026-06-11) — 자동 산정 + 지급 감액."""
    from datetime import date as _d

    from erp.hr.models import LeaveRequest
    emp = _emp("E0230", "박무급")
    session.add(emp)
    await session.flush()
    session.add(LeaveRequest(employee_id=emp.id, leave_type="UNPAID",
                             start_on=_d(2026, 7, 6), end_on=_d(2026, 7, 7),
                             days=2, status="APPROVED"))
    session.add(LeaveRequest(employee_id=emp.id, leave_type="ANNUAL",   # 유급 — 미공제
                             start_on=_d(2026, 7, 10), end_on=_d(2026, 7, 10),
                             days=1, status="APPROVED"))
    await session.flush()

    auto = await resolve_monthly_inputs(session, emp, YM)
    assert auto["unpaid_days"] == 2.0

    r = payroll_rules.build_payroll(employment_type="REGULAR", base_pay=2_090_000,
                                    unpaid_days=auto["unpaid_days"])
    unpaid = next(line for line in r["lines"] if line["item_code"] == "UNPAID_LEAVE")
    assert unpaid["amount"] == -160_000          # 통상일급 80,000 × 2일 차감
    assert r["gross"] == 2_090_000 - 160_000     # 보험·세금도 감액 보수 기준
