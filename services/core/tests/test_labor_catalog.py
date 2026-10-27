"""노동규제 룰 관리 (FR-32) — 시프트 카탈로그·임계값 조회/수정 + duty_service 반영."""
from datetime import date, timedelta

import pytest

from erp.errors import ValidationError
from erp.hr.duty_service import DutyCell, DutyService
from erp.hr.labor_catalog import (
    list_labor_rules,
    list_shift_types,
    upsert_labor_rule,
    upsert_shift_type,
)
from erp.hr.models import Employee

svc = DutyService()


async def _emp(session, no="R001", group="RESIDENT"):
    e = Employee(employee_no=no, name=f"직원{no}", hired_on=date(2024, 1, 1),
                 department_id=10, job_group=group)
    session.add(e)
    await session.flush()
    return e


# ── 시프트 카탈로그 ──────────────────────────────────────────
async def test_shift_list_defaults_then_overlay(session):
    # 미등록 → 코드 기본값 D/E/N/OFF 노출
    items = {i["code"]: i for i in await list_shift_types(session)}
    assert set(items) >= {"D", "E", "N", "OFF"}
    assert items["N"]["source"] == "기본값" and items["N"]["is_night"] is True
    assert items["OFF"]["counts_as_workday"] is False

    # 당직(OC 24h) 등록 → DB 오버레이로 노출
    await upsert_shift_type(session, actor=1, code="OC", label="당직",
                            work_hours=24, is_night=True, counts_as_workday=True)
    items2 = {i["code"]: i for i in await list_shift_types(session)}
    assert items2["OC"]["source"] == "DB" and items2["OC"]["work_hours"] == 24.0


async def test_shift_times_roundtrip_and_validation(session):
    """근태 판정 시각(HH:MM) 저장·조회 + 형식 검증(전체검토 4)."""
    await upsert_shift_type(session, actor=1, code="OC", label="당직", work_hours=12,
                            is_night=True, counts_as_workday=True,
                            start_time="08:00", end_time="20:00")
    items = {i["code"]: i for i in await list_shift_types(session)}
    assert items["OC"]["start_time"] == "08:00" and items["OC"]["end_time"] == "20:00"
    # 코드 기본 시프트(D)는 attendance 기본 시각 노출
    assert items["D"]["start_time"] == "07:00" and items["D"]["end_time"] == "15:00"
    with pytest.raises(ValidationError):
        await upsert_shift_type(session, actor=1, code="ZZ", label="x", work_hours=8,
                                is_night=False, counts_as_workday=True, start_time="9999")


async def test_shift_registration_activates_80h_rule(session):
    """8h-only면 80h 미발화, OC 24h 등록 시 전공의 4주 80h 룰 부활."""
    emp = await _emp(session, group="RESIDENT")
    cells = [DutyCell(emp.id, date(2026, 7, 6) + timedelta(days=i), "OC") for i in range(28)]

    # 카탈로그 미등록 — OC는 폴백에 없어 거부됨(검증)
    with pytest.raises(ValidationError):
        await svc.save_month(session, department_id=10, yyyymm="2026-07", cells=cells)

    # OC 24h 등록 후 → 4주 평균 주 80h 초과 발화(RESIDENT_AVG_HOURS)
    await upsert_shift_type(session, actor=1, code="OC", label="당직",
                            work_hours=24, is_night=False, counts_as_workday=True)
    _, violations = await svc.save_month(session, department_id=10, yyyymm="2026-07",
                                         cells=cells)
    assert any(v.rule == "RESIDENT_AVG_HOURS" for v in violations)


async def test_shift_validation(session):
    with pytest.raises(ValidationError):
        await upsert_shift_type(session, actor=1, code="X", label="x",
                                work_hours=25, is_night=False, counts_as_workday=True)
    with pytest.raises(ValidationError):
        await upsert_shift_type(session, actor=1, code="", label="x",
                                work_hours=8, is_night=False, counts_as_workday=True)


# ── 노동규제 임계값 ──────────────────────────────────────────
async def test_rule_list_defaults_and_override(session):
    items = {i["rule_key"]: i for i in await list_labor_rules(session)}
    assert items["resident_avg_4week"]["threshold"] == 80.0
    assert items["resident_avg_4week"]["overridden"] is False

    await upsert_labor_rule(session, actor=1, rule_key="resident_avg_4week",
                            threshold=72, notice_ref="기관 정책")
    items2 = {i["rule_key"]: i for i in await list_labor_rules(session)}
    assert items2["resident_avg_4week"]["threshold"] == 72.0
    assert items2["resident_avg_4week"]["overridden"] is True
    assert items2["resident_avg_4week"]["default"] == 80.0


async def test_rule_override_affects_evaluation(session):
    """임계값을 낮추면 평소 통과하던 패턴도 위반 발화 (일반직 주간룰)."""
    emp = await _emp(session, no="N001", group="NURSE")
    # 주 6일 8h = 48h (일반 52h 미만, 정상)
    cells = [DutyCell(emp.id, date(2026, 7, 6) + timedelta(days=i), "D") for i in range(6)]
    _, v0 = await svc.save_month(session, department_id=10, yyyymm="2026-07", cells=cells)
    assert not any(x.rule == "WEEKLY_HOURS" for x in v0)

    # 임계값을 40h로 강화 → 48h가 위반
    await upsert_labor_rule(session, actor=1, rule_key="weekly_hours_general", threshold=40)
    _, v1 = await svc.save_month(session, department_id=10, yyyymm="2026-07", cells=cells)
    assert any(x.rule == "WEEKLY_HOURS" for x in v1)


async def test_rule_validation(session):
    with pytest.raises(ValidationError):
        await upsert_labor_rule(session, actor=1, rule_key="weekly_hours_general", threshold=0)
