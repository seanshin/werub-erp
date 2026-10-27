"""듀티 편성 (FR-06) + 노동규제 경고 (FR-32)."""
from datetime import date, timedelta

from erp.hr.duty_service import DutyCell, DutyService
from erp.hr.models import Employee, ShiftType

svc = DutyService()


async def _emp(session, no="N001", group="NURSE"):
    e = Employee(employee_no=no, name=f"직원{no}", hired_on=date(2024, 1, 1),
                 department_id=10, job_group=group)
    session.add(e)
    await session.flush()
    return e


async def _shift(session, code="OC", hours=24, is_night=True, workday=True):
    """카탈로그에 시프트 등록(당직 등 장시간)."""
    session.add(ShiftType(code=code, label=f"시프트{code}", work_hours=hours,
                          is_night=is_night, counts_as_workday=workday))
    await session.flush()


def _month_cells(emp_id: int, shifts: list[str], start=date(2026, 7, 6)) -> list[DutyCell]:
    """7/6(월)부터 연속 배정."""
    return [DutyCell(emp_id, start + timedelta(days=i), s) for i, s in enumerate(shifts)]


async def test_save_and_resave_overwrites(session):
    emp = await _emp(session)
    cells = _month_cells(emp.id, ["D", "E", "N", "OFF"])
    schedule, _ = await svc.save_month(session, department_id=10, yyyymm="2026-07", cells=cells)
    schedule2, _ = await svc.save_month(session, department_id=10, yyyymm="2026-07",
                                        cells=cells[:2])
    assert schedule.id == schedule2.id  # 동일 월 덮어쓰기


async def test_no_violation_for_normal_pattern(session):
    emp = await _emp(session)
    _, violations = await svc.save_month(
        session, department_id=10, yyyymm="2026-07",
        cells=_month_cells(emp.id, ["D", "D", "E", "OFF", "N", "OFF", "D"]))
    assert violations == []


async def test_weekly_52h_warning_for_general(session):
    """주 52시간 초과 — 일반 직군 경고 (월~일 7일 연속 8h = 56h)."""
    emp = await _emp(session, group="NURSE")
    _, violations = await svc.save_month(
        session, department_id=10, yyyymm="2026-07",
        cells=_month_cells(emp.id, ["D"] * 7))  # 56h
    rules = {v.rule for v in violations}
    assert "WEEKLY_HOURS" in rules
    assert any("근로기준법" in v.detail for v in violations)


async def test_resident_80h_limit_applied(session):
    """전공의는 주 80시간 기준 — 56h는 경고 없음 (전공의법, FR-32)."""
    emp = await _emp(session, no="R001", group="RESIDENT")
    _, violations = await svc.save_month(
        session, department_id=10, yyyymm="2026-07",
        cells=_month_cells(emp.id, ["D"] * 7))  # 56h < 80h
    assert all(v.rule != "WEEKLY_HOURS" for v in violations)


async def test_consecutive_nights_warning(session):
    emp = await _emp(session)
    _, violations = await svc.save_month(
        session, department_id=10, yyyymm="2026-07",
        cells=_month_cells(emp.id, ["N", "N", "N", "N"]))  # 연속 4야간 > 3
    assert any(v.rule == "CONSECUTIVE_NIGHTS" for v in violations)


async def test_violation_does_not_block_save(session):
    """FR-32 — 경고는 저장을 막지 않음."""
    emp = await _emp(session)
    schedule, violations = await svc.save_month(
        session, department_id=10, yyyymm="2026-07",
        cells=_month_cells(emp.id, ["N"] * 10))
    assert schedule.id is not None and len(violations) > 0


async def test_consecutive_workdays_warning(session):
    """연속 근무일 6일 초과 경고 (근로기준법 §55 주휴 취지) — 연속근무일 룰 격리 검증.

    목요일부터 7일 연속(주 경계 분할: 4일+3일 = 주별 ≤32h)이라 52h 룰은 미발화,
    연속근무일(>6)만 발화하도록 분리."""
    emp = await _emp(session)
    _, violations = await svc.save_month(
        session, department_id=10, yyyymm="2026-07",
        cells=_month_cells(emp.id, ["D"] * 7, start=date(2026, 7, 9)))  # 목~ 7일 연속
    rules = {v.rule for v in violations}
    assert "CONSECUTIVE_WORKDAYS" in rules
    assert "WEEKLY_HOURS" not in rules    # 주별 ≤4일(≤32h) — 52h 미초과


async def test_resident_80h_not_fired_with_8h_only_shifts(session):
    """8h 시프트(D/E/N)만으로는 전공의 80h 룰 미발화 — 주 최대 56h라 정상(오탐 없음).

    (과거엔 이것이 '죽은 룰'의 한계였으나, 이제 당직 시프트 등록으로 실효화됨 → 아래 테스트)."""
    emp = await _emp(session, no="R-MAX", group="RESIDENT")
    _, violations = await svc.save_month(
        session, department_id=10, yyyymm="2026-07",
        cells=_month_cells(emp.id, ["D"] * 28))
    assert all(v.rule != "RESIDENT_AVG_HOURS" for v in violations)


async def test_resident_80h_fires_with_oncall_shift(session):
    """✅ 죽은 룰 되살리기 — 당직(OC 24h) 카탈로그 등록 시 전공의 80h 룰 실제 발화.

    한 주 OC·OC·OC·D·D·OFF·OFF = 24×3+8×2 = 88h(>80) → RESIDENT_AVG_HOURS 발화."""
    await _shift(session, code="OC", hours=24)  # 당직 등록
    emp = await _emp(session, no="R-OC", group="RESIDENT")
    _, violations = await svc.save_month(
        session, department_id=10, yyyymm="2026-07",
        cells=_month_cells(emp.id, ["OC", "OC", "OC", "D", "D", "OFF", "OFF"]))  # 88h/주
    assert any(v.rule == "RESIDENT_AVG_HOURS" for v in violations)
    assert any("88" in v.detail for v in violations)


async def test_resident_avg_excludes_partial_boundary_weeks(session):
    """완전주(7일)만 평균 분모 — 월경계 부분주가 평균을 희석해 위반을 가리지 않아야 한다(전체검토 3).

    7/5(일·1일=부분주) + 7/6~8/2(완전 4주·각 12h×7=84h) + 8/3(월·1일=부분주).
    단순평균=(12+84×4+12)/6=60≤80(과거 미발화) / 완전주평균=84>80(발화)."""
    await _shift(session, code="T", hours=12, is_night=False, workday=True)
    emp = await _emp(session, no="R-PART", group="RESIDENT")
    _, violations = await svc.save_month(
        session, department_id=10, yyyymm="2026-07",
        cells=_month_cells(emp.id, ["T"] * 30, start=date(2026, 7, 5)))
    avg_v = [v for v in violations if v.rule == "RESIDENT_AVG_HOURS"]
    assert avg_v and "4주" in avg_v[0].detail and "84" in avg_v[0].detail


async def test_resident_single_week_max_fires(session):
    """단일주 절대상한 — 평균이 통과해도 한 주 스파이크는 RESIDENT_WEEK_MAX로 적발(전체검토 3).

    한 주에 OC(24h) 4일 = 96h(>88). 부분주라 평균(완전주) 미산정이지만 단일주 상한은 발화."""
    await _shift(session, code="OC", hours=24)
    emp = await _emp(session, no="R-SPK", group="RESIDENT")
    _, violations = await svc.save_month(
        session, department_id=10, yyyymm="2026-07",
        cells=_month_cells(emp.id, ["OC"] * 4))  # 96h, 4일(부분주)
    rules = {v.rule for v in violations}
    assert "RESIDENT_WEEK_MAX" in rules
    assert "RESIDENT_AVG_HOURS" not in rules     # 완전주 없음 → 평균 미산정


async def test_unknown_shift_code_rejected(session):
    """카탈로그에 없는 코드는 거부 — shift_types가 SoT."""
    import pytest

    from erp.errors import ValidationError
    emp = await _emp(session)
    with pytest.raises(ValidationError):
        await svc.save_month(session, department_id=10, yyyymm="2026-07",
                             cells=_month_cells(emp.id, ["XX"]))


async def test_db_threshold_override(session):
    """labor_rules 적재 시 임계값 오버라이드 — 일반직 주 한도를 40h로 낮추면 48h가 위반."""
    from erp.regulatory.models import LaborRule
    session.add(LaborRule(rule_key="weekly_hours_general", threshold=40,
                          notice_ref="테스트 오버라이드"))
    await session.flush()
    emp = await _emp(session, group="NURSE")
    _, violations = await svc.save_month(
        session, department_id=10, yyyymm="2026-07",
        cells=_month_cells(emp.id, ["D"] * 6))  # 48h > 40h(오버라이드)
    assert any(v.rule == "WEEKLY_HOURS" for v in violations)
