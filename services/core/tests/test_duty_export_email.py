"""Track A — 근무표 Excel export(FR-25) + 이메일 알림(FR-27)."""
from datetime import date


from erp.common.email import send_email
from erp.common.router import CurrentUser
from erp.config import get_settings
from erp.hr.models import DutyAssignment, DutySchedule, Employee
from erp.hr.router import export_duties

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=1, license_flag=True)


async def _seed_duty(session):
    emp = Employee(employee_no="E0001", name="이지호", hired_on=date(2020, 1, 1),
                   department_id=1, job_group="NURSE")
    session.add(emp)
    sched = DutySchedule(department_id=1, yyyymm="2026-05")
    session.add(sched)
    await session.flush()
    for day, shift in [(1, "D"), (2, "E"), (3, "N"), (4, "OFF")]:
        session.add(DutyAssignment(schedule_id=sched.id, employee_id=emp.id,
                                   duty_on=date(2026, 5, day), shift=shift))
    await session.flush()


async def test_duty_export_xlsx(session):
    await _seed_duty(session)
    resp = await export_duties(yyyymm="2026-05", department_id=None, user=ADMIN, session=session)
    assert "spreadsheetml" in resp.media_type
    assert "duty-2026-05.xlsx" in resp.headers["content-disposition"]
    body = b"".join([chunk async for chunk in resp.body_iterator])
    assert body[:2] == b"PK" and len(body) > 100   # 유효 xlsx(zip)


async def test_duty_export_empty_month(session):
    """듀티 없는 월 → 빈 그리드라도 유효 xlsx."""
    resp = await export_duties(yyyymm="2026-12", department_id=None, user=ADMIN, session=session)
    body = b"".join([chunk async for chunk in resp.body_iterator])
    assert body[:2] == b"PK"


def test_email_noop_when_smtp_unset():
    """SMTP 미설정 시 발송 생략(no-op) — False, 예외 없음."""
    assert get_settings().smtp_host is None
    assert send_email("x@hospital.local", "테스트", "본문") is False
