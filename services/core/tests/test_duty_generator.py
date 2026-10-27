"""자동 근무표 생성 (FR-45) — 직군 템플릿 패턴·커버리지·야간 후 휴식."""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.errors import ValidationError
from erp.hr.duty_generator import DutyGenerator
from erp.hr.models import DutyAssignment, DutySchedule, Employee

gen = DutyGenerator()


async def _seed_nurses(session, n=3, dept_id=1):
    for i in range(n):
        session.add(Employee(employee_no=f"N{i:03d}", name=f"간호{i}", hired_on=date(2020, 1, 1),
                             department_id=dept_id, job_group="NURSE"))
    await session.flush()


async def test_generate_creates_roster(session):
    await _seed_nurses(session, 3)
    schedule, violations, headcount = await gen.generate(
        session, department_id=1, yyyymm="2026-05")
    assert headcount == 3 and schedule.status == "DRAFT"
    cnt = await session.scalar(
        select(func.count()).select_from(DutyAssignment)
        .where(DutyAssignment.schedule_id == schedule.id))
    assert cnt == 3 * 31  # 3명 × 5월 31일


async def test_pattern_offset_and_rest_after_night(session):
    await _seed_nurses(session, 3)
    await gen.generate(session, department_id=1, yyyymm="2026-05")
    e0 = (await session.execute(
        select(Employee).where(Employee.employee_no == "N000"))).scalar_one()
    by_day = {a.duty_on.day: a.shift for a in (await session.execute(
        select(DutyAssignment).where(DutyAssignment.employee_id == e0.id))).scalars().all()}
    # 패턴 D D E E N N OFF OFF (오프셋 0)
    assert by_day[1] == "D" and by_day[3] == "E"
    assert by_day[5] == "N" and by_day[6] == "N" and by_day[7] == "OFF"  # 야간 후 휴식


async def test_regenerate_overwrites(session):
    await _seed_nurses(session, 2)
    s1, _, _ = await gen.generate(session, department_id=1, yyyymm="2026-05")
    s2, _, _ = await gen.generate(session, department_id=1, yyyymm="2026-05")  # 재생성
    assert s1.id == s2.id  # 동일 스케줄 덮어쓰기
    n_sched = await session.scalar(select(func.count()).select_from(DutySchedule))
    assert n_sched == 1


async def test_no_target_employees_raises(session):
    with pytest.raises(ValidationError, match="대상 직원이 없습니다"):
        await gen.generate(session, department_id=99, yyyymm="2026-05")
