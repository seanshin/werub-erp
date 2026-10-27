"""직급·호봉 + 징계·상벌 (P3-8) — 호봉표 upsert/lookup·상벌 등록·이력·검증."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.hr.models import Employee
from erp.hr.personnel_service import DisciplinaryService, PayGradeService

pg = PayGradeService()
disc = DisciplinaryService()


async def _emp(session, no):
    e = Employee(employee_no=no, name=f"직원{no}", hired_on=date(2025, 1, 1), job_group="STAFF",
                 employment_type="REGULAR", source="ERP")
    session.add(e)
    await session.flush()
    return e


async def test_paygrade_upsert_and_lookup(session):
    await pg.upsert_step(session, grade="5급", step=1, base_pay=3000000, effective_year=2026)
    await pg.upsert_step(session, grade="5급", step=1, base_pay=3100000, effective_year=2026)  # 갱신
    r = await pg.lookup(session, grade="5급", step=1, year=2026)
    assert r["base_pay"] == 3100000
    rows = await pg.list_steps(session, effective_year=2026)
    assert len(rows) == 1                          # 멱등(중복 행 없음)
    assert await pg.lookup(session, grade="5급", step=2, year=2026) is None


async def test_paygrade_year_isolation(session):
    await pg.upsert_step(session, grade="4급", step=1, base_pay=3500000, effective_year=2026)
    await pg.upsert_step(session, grade="4급", step=1, base_pay=3600000, effective_year=2027)
    assert (await pg.lookup(session, grade="4급", step=1, year=2026))["base_pay"] == 3500000
    assert (await pg.lookup(session, grade="4급", step=1, year=2027))["base_pay"] == 3600000


async def test_paygrade_guards(session):
    with pytest.raises(ValidationError):
        await pg.upsert_step(session, grade="", step=1, base_pay=100, effective_year=2026)
    with pytest.raises(ValidationError):
        await pg.upsert_step(session, grade="5급", step=0, base_pay=100, effective_year=2026)


async def test_disciplinary_record_and_history(session):
    e = await _emp(session, "D1")
    await disc.record(session, employee_id=e.id, action_type="DISCIPLINE", category="견책",
                      reason="복무규정 위반", decided_on=date(2026, 3, 1))
    await disc.record(session, employee_id=e.id, action_type="REWARD", category="표창",
                      reason="우수 직원", decided_on=date(2026, 6, 1))
    hist = await disc.list_by_employee(session, employee_id=e.id)
    assert len(hist) == 2 and hist[0]["decided_on"] == "2026-06-01"   # 최신순
    rewards = await disc.recent(session, action_type="REWARD")
    assert all(x["action_type"] == "REWARD" for x in rewards)


async def test_disciplinary_guards(session):
    e = await _emp(session, "D2")
    with pytest.raises(ValidationError):
        await disc.record(session, employee_id=e.id, action_type="BAD", category="x",
                          reason="y", decided_on=date(2026, 1, 1))
    with pytest.raises(ValidationError):
        await disc.record(session, employee_id=999999, action_type="DISCIPLINE", category="견책",
                          reason="y", decided_on=date(2026, 1, 1))
    with pytest.raises(ValidationError):
        await disc.record(session, employee_id=e.id, action_type="DISCIPLINE", category="정직",
                          reason="z", decided_on=date(2026, 1, 1),
                          effective_from=date(2026, 2, 1), effective_to=date(2026, 1, 15))
