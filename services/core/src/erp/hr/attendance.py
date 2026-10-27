"""근태 집계 소비 (FR-29) — integration 스테이징 → hr.attendance_summaries.

설계 §5.2: 코어(hr) 쓰기는 스테이징 소비 단계에서만. 멱등 — 영향 (사번×월)을
스테이징 전량으로 재계산(absolute upsert)하므로 재실행/재처리에 안전.
"""
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.kst import as_kst_wallclock

from erp.hr.models import (
    AttendanceSummary,
    DutyAssignment,
    Employee,
    GroupwareAttendanceStaging,
    ShiftType,
)

logger = logging.getLogger("erp.hr.attendance")

NIGHT_START_HOUR = 22   # 야간 판정(듀티 미배정 시 폴백): 출근 22시 이후 or 퇴근 06시 이전
NIGHT_END_HOUR = 6

# 표준 3교대 근무시간 (FR-29 지각/조퇴 판정 기준) — 병원별 상이, 코드 기본값(폴백).
# 인사팀이 hr.shift_types에 start_time/end_time을 등록하면 그 값이 우선(커스텀 시프트 판정 활성화).
SHIFT_TIMES = {
    "D": (time(7, 0), time(15, 0)),
    "E": (time(15, 0), time(23, 0)),
    "N": (time(23, 0), time(7, 0)),   # 익일 종료
}
_CODE_NIGHT = {"N"}                         # 코드 기본 야간 시프트(폴백)
ATTENDANCE_GRACE = timedelta(minutes=10)   # 지각/조퇴 유예


def _yyyymm(d) -> str:
    return d.strftime("%Y-%m")


async def _load_shift_catalog(session: AsyncSession) -> dict[str, dict]:
    """code → {times:(start,end)|None, is_night}. 코드 기본값 위에 hr.shift_types(active) 오버레이.

    근태 판정 SoT 단일화 — 커스텀 시프트(당직 OC 등)도 등록 시각·야간플래그로 판정.
    """
    cat = {c: {"times": SHIFT_TIMES.get(c), "is_night": c in _CODE_NIGHT} for c in SHIFT_TIMES}
    for r in (await session.scalars(select(ShiftType).where(ShiftType.active.is_(True)))).all():
        times = ((r.start_time, r.end_time) if r.start_time and r.end_time
                 else SHIFT_TIMES.get(r.code))   # 시각 미등록 시 코드 기본값 폴백
        cat[r.code] = {"times": times, "is_night": r.is_night}
    return cat


def _shift_judgment(duty_on, shift: str, clock_in, clock_out, catalog: dict) -> tuple[bool, bool]:
    """듀티 교대 시각 대비 지각·조퇴 판정. 카탈로그 시각 우선, 비교는 적재된 wall-clock 기준."""
    info = catalog.get(shift)
    times = info["times"] if info else SHIFT_TIMES.get(shift)
    if times is None:                       # OFF·시각 미정의 → 판정 불가
        return False, False
    start_t, end_t = times
    tz = (clock_in or clock_out).tzinfo if (clock_in or clock_out) else None
    start_dt = datetime.combine(duty_on, start_t, tzinfo=tz)
    end_day = duty_on + timedelta(days=1) if end_t <= start_t else duty_on  # N교대 익일
    end_dt = datetime.combine(end_day, end_t, tzinfo=tz)
    late = clock_in is not None and clock_in > start_dt + ATTENDANCE_GRACE
    early = clock_out is not None and clock_out < end_dt - ATTENDANCE_GRACE
    return late, early


def _aggregate(rows: list[GroupwareAttendanceStaging], duty_by: dict, catalog: dict) -> dict:
    """duty_by: {duty_on(date) → shift} (해당 직원 월 듀티). 없으면 지각/조퇴 미판정.

    야간: 배정 시프트가 있으면 카탈로그 is_night(커스텀 시프트 포함), 없으면 출퇴근 시각 휴리스틱.
    """
    work_days = night = late = early = 0
    minutes = 0
    for r in rows:
        # KST 벽시계 naive로 통일 — PG(asyncpg)는 진짜 UTC aware로 되읽히므로 KST 환산 필수(라벨-strip은 -9h 왜곡·v1.257.2)
        ci, co = as_kst_wallclock(r.clock_in), as_kst_wallclock(r.clock_out)
        if ci is not None:
            work_days += 1
        if ci is not None and co is not None:
            minutes += int((co - ci).total_seconds() // 60)
        shift = duty_by.get(r.work_on)
        info = catalog.get(shift) if shift else None
        if info is not None:                # 배정 시프트 → 카탈로그 야간 플래그
            if info["is_night"]:
                night += 1
        else:                               # 미배정 → 출퇴근 시각 휴리스틱(폴백)
            ci_night = ci is not None and ci.hour >= NIGHT_START_HOUR
            co_night = co is not None and co.hour <= NIGHT_END_HOUR
            if ci_night or co_night:
                night += 1
        if shift:
            is_late, is_early = _shift_judgment(r.work_on, shift, ci, co, catalog)
            late += int(is_late)
            early += int(is_early)
    return {"work_days": work_days, "night_shift_count": night,
            "total_work_minutes": minutes,
            "late_count": late, "early_leave_count": early}


async def consume_attendance_staging(session: AsyncSession, *, limit: int = 5000) -> dict:
    """NEW 스테이징 소비 → 사번 해소 → (사번×월) 재집계 upsert → APPLIED 마킹.

    반환: {applied, summaries, unmapped} — unmapped은 사번 미해소로 FAILED 처리된 건수.
    """
    new_rows = (await session.scalars(
        select(GroupwareAttendanceStaging)
        .where(GroupwareAttendanceStaging.status == "NEW")
        .order_by(GroupwareAttendanceStaging.id)
        .limit(limit))).all()
    if not new_rows:
        return {"applied": 0, "summaries": 0, "unmapped": 0}

    # 사번 → employee_id 해소 (한 번에 조회)
    emp_nos = {r.employee_no for r in new_rows if r.employee_no}
    id_by_no: dict[str, int] = {}
    if emp_nos:
        for emp_no, emp_id in (await session.execute(
                select(Employee.employee_no, Employee.id)
                .where(Employee.employee_no.in_(emp_nos)))).all():
            id_by_no[emp_no] = emp_id

    applied = unmapped = 0
    affected: set[tuple[int, str, str]] = set()  # (employee_id, employee_no, yyyymm)
    for r in new_rows:
        emp_id = id_by_no.get(r.employee_no or "")
        if emp_id is None:
            r.status = "FAILED"
            r.error_detail = {"reason": "unmapped_employee_no", "employee_no": r.employee_no}
            unmapped += 1
            continue
        r.status = "APPLIED"
        applied += 1
        affected.add((emp_id, r.employee_no, _yyyymm(r.work_on)))

    # 영향 (사번×월)을 스테이징 전량(APPLIED)으로 재계산 → 절대값 upsert (멱등)
    summaries = 0
    by_key: dict[tuple[int, str], list[GroupwareAttendanceStaging]] = defaultdict(list)
    if affected:
        emp_nos_aff = {no for _, no, _ in affected}
        all_applied = (await session.scalars(
            select(GroupwareAttendanceStaging).where(
                GroupwareAttendanceStaging.status == "APPLIED",
                GroupwareAttendanceStaging.employee_no.in_(emp_nos_aff)))).all()
        for row in all_applied:
            by_key[(row.employee_no, _yyyymm(row.work_on))].append(row)

    # 듀티 로드 (지각/조퇴 판정용) — 영향 직원·일자에 한정
    duty_by_emp: dict[int, dict] = defaultdict(dict)
    affected_ids = {emp_id for emp_id, _, _ in affected}
    all_dates = {row.work_on for rows in by_key.values() for row in rows}
    if affected_ids and all_dates:
        for emp_id, duty_on, shift in (await session.execute(
                select(DutyAssignment.employee_id, DutyAssignment.duty_on, DutyAssignment.shift)
                .where(DutyAssignment.employee_id.in_(affected_ids),
                       DutyAssignment.duty_on.in_(all_dates)))).all():
            duty_by_emp[emp_id][duty_on] = shift

    # 기존 요약 일괄 선로드 (N+1 제거) — 영향 (직원,월) 조합을 한 번에
    existing_map: dict[tuple[int, str], AttendanceSummary] = {}
    if affected:
        rows = (await session.scalars(select(AttendanceSummary).where(
            AttendanceSummary.employee_id.in_({a[0] for a in affected}),
            AttendanceSummary.yyyymm.in_({a[2] for a in affected})))).all()
        existing_map = {(s.employee_id, s.yyyymm): s for s in rows}

    catalog = await _load_shift_catalog(session)   # 시프트 시각·야간 SoT(커스텀 포함)
    for emp_id, emp_no, yyyymm in affected:
        agg = _aggregate(by_key.get((emp_no, yyyymm), []), duty_by_emp.get(emp_id, {}), catalog)
        existing = existing_map.get((emp_id, yyyymm))
        if existing is None:
            session.add(AttendanceSummary(employee_id=emp_id, yyyymm=yyyymm, **agg))
        else:
            for k, v in agg.items():
                setattr(existing, k, v)
        summaries += 1

    logger.info("근태 집계 소비", extra={"ctx": {
        "applied": applied, "summaries": summaries, "unmapped": unmapped}})
    return {"applied": applied, "summaries": summaries, "unmapped": unmapped}
