"""근태·듀티 → 급여 월 변동분 자동 산정 (인사 P1-②).

기존: generate 호출 시 night_hours·duty_count·work_days를 매월 수동 입력.
변경: AttendanceSummary(그룹웨어 근태 집계)·DutyAssignment(듀티 N 배정)에서 자동 해소.
명시 입력이 있으면 항상 우선(수동 보정 경로 유지).

산정 규칙(운영 조정 대상 상수):
- 야간시간(간호사) = night_shift_count × 8h
- 당직횟수(의사·전공의) = 해당월 듀티 N 배정 수
- 연장시간 = max(0, 총근무시간 − 8h × 근무일수) — 통상시급×1.5 가산은 룰엔진에서
- 휴일근로(H-2) = 일별 스테이징에서 휴일(일요일+법정공휴일) 근무시간 도출 —
  일 8h 이내(×1.5)·초과(×2.0) 분리, 평일 연장 집계에서 휴일 근무일 제외(이중지급 방지)
- 일용 근무일수 = 근태 집계 work_days
"""
import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.kst import as_kst_wallclock
from erp.common.months import canonical_ym
from erp.hr.models import (
    AttendanceSummary, DutyAssignment, DutySchedule, Employee,
    GroupwareAttendanceStaging, LeaveRequest,
)

logger = logging.getLogger("erp.hr.pay_inputs")

NIGHT_SHIFT_HOURS = 8           # N 교대 1회 야간 인정 시간
DAILY_STANDARD_HOURS = 8        # 1일 소정근로 — 초과분이 연장


async def _month_holidays(session: AsyncSession, y: int, m: int) -> set[date]:
    """해당월 휴일(휴일근로 가산 대상) = 일요일 + 법정공휴일(고정양력+DB 캘린더+코드 폴백).

    토요일은 통상 무급휴무일(연장근로 대상)이라 제외 — 사전심사 공휴가산과 동일 규약.
    """
    from calendar import monthrange

    from erp.claims import deadline
    from erp.common.holidays import HolidayService
    db_dates = await HolidayService().dates(session, year=y)
    out: set[date] = set()
    for day in range(1, monthrange(y, m)[1] + 1):
        d = date(y, m, day)
        if (d.weekday() == 6 or deadline.is_fixed_holiday(d)
                or d in db_dates
                or (d.month, d.day) in deadline.EXTRA_HOLIDAYS.get(y, set())):
            out.add(d)
    return out


def _saturday_ot_split(rows, holidays: set[date], scheduled: set[date]) -> tuple[float, int]:
    """토요일(무급휴무일) 근무 → 연장근로 시간·일수. 단, 듀티 배정된 토요일은 제외.

    사용자 결정(근무표 기반 판정): 근무표(듀티)에 배정된 토요일은 소정근로일이라 평일 기준시간에
    흡수(교대제 간호사 등), 미배정 토요일 근무는 무급휴무일 연장근로(주40시간 근로자 미지급 해소·H-B).
    일요일·공휴일은 _holiday_split(휴일근로 가산)이 담당하므로 여기선 토요일만.
    """
    hours = 0.0
    days = 0
    for r in rows:
        d = r.work_on
        if d.weekday() != 5 or d in holidays or d in scheduled:
            continue
        ci, co = as_kst_wallclock(r.clock_in), as_kst_wallclock(r.clock_out)
        if ci is None or co is None:
            continue
        h = max(0.0, (co - ci).total_seconds() / 3600.0)
        if h <= 0:
            continue
        days += 1
        hours += h
    return hours, days


def _holiday_split(rows, holidays: set[date]) -> tuple[float, float, int]:
    """스테이징 일자행 → (휴일 8h이내 합, 휴일 8h초과 합, 휴일 근무일수).

    근기법 §56②③: 휴일근로 8h 이내 50% 가산(×1.5), 초과 100%(×2.0) — 일 단위 분리.
    clock_out 누락 휴일행도 휴일 근무일수(days)에는 계상 — 미계상 시 그 날이 평일 기준시간(8h×근무일)에
    흡수돼 평일 연장이 최대 8h 과소산정(적대검토 L-4 데이터품질). 시간 미상이라 가산액엔 미반영(경고 로그).
    """
    within = over = 0.0
    days = 0
    for r in rows:
        if r.work_on not in holidays:
            continue
        ci, co = as_kst_wallclock(r.clock_in), as_kst_wallclock(r.clock_out)
        if ci is None:
            continue
        if co is None:
            # 출근만 있고 퇴근 미기록 — 휴일 근무일로 계상(평일 기준 흡수 방지)하되 시간 미상
            days += 1
            logger.warning("휴일 근무 clock_out 누락 — 가산액 미반영(데이터 보정 필요)",
                           extra={"ctx": {"employee_no": getattr(r, "employee_no", None),
                                          "work_on": r.work_on.isoformat()}})
            continue
        hours = max(0.0, (co - ci).total_seconds() / 3600.0)
        if hours <= 0:
            continue
        days += 1
        within += min(hours, float(DAILY_STANDARD_HOURS))
        over += max(0.0, hours - DAILY_STANDARD_HOURS)
    return within, over, days


def _ym_dash(yyyymm: str) -> str:
    """"202607"·"2026-07" 모두 "2026-07"로 정규화 (집계 테이블 표기·단일 소스 common.months)."""
    return canonical_ym(yyyymm)


async def resolve_monthly_inputs(session: AsyncSession, emp: Employee,
                                 yyyymm: str) -> dict:
    """직원×월 급여 변동분 — {work_days, night_hours, duty_count, ot_hours}."""
    ym = _ym_dash(yyyymm)
    att = (await session.execute(
        select(AttendanceSummary).where(AttendanceSummary.employee_id == emp.id,
                                        AttendanceSummary.yyyymm == ym))).scalar_one_or_none()
    work_days = att.work_days if att else 0
    night_hours = 0.0
    ot_hours = 0.0
    # 휴일근로(H-2) — 일별 스테이징에서 휴일 근무시간 도출(8h이내/초과 분리)
    y0, m0 = int(ym[:4]), int(ym[5:7])
    holidays = await _month_holidays(session, y0, m0)
    stg = (await session.scalars(
        select(GroupwareAttendanceStaging).where(
            GroupwareAttendanceStaging.employee_no == emp.employee_no,
            GroupwareAttendanceStaging.work_on >= date(y0, m0, 1),
            GroupwareAttendanceStaging.work_on < date(y0 + (m0 == 12), (m0 % 12) + 1, 1)))).all()
    hol_within, hol_over, hol_days = _holiday_split(stg, holidays)
    # 토요일(무급휴무일) 근로 판정 — 듀티 배정된 토요일은 소정근로일(교대제)이라 제외(사용자 결정: 근무표 기반)
    scheduled = set((await session.scalars(
        select(DutyAssignment.duty_on).where(
            DutyAssignment.employee_id == emp.id,
            DutyAssignment.duty_on >= date(y0, m0, 1),
            DutyAssignment.duty_on < date(y0 + (m0 == 12), (m0 % 12) + 1, 1)))).all())
    sat_ot_hours, sat_ot_days = _saturday_ot_split(stg, holidays, scheduled)
    if att:
        if emp.job_category == "NURSE":
            night_hours = float(att.night_shift_count * NIGHT_SHIFT_HOURS)
        # 평일 연장 — 휴일·무급휴무일(미배정 토요일) 근무시간·근무일은 기준에서 제외(이중지급 방지),
        # 미배정 토요일 근로는 전부 연장근로로 가산(H-B — 주40시간 근로자 토요일 8h 소멸 해소).
        ot_hours = max(0.0, att.total_work_minutes / 60.0 - (hol_within + hol_over) - sat_ot_hours
                       - DAILY_STANDARD_HOURS * max(0, att.work_days - hol_days - sat_ot_days)
                       ) + sat_ot_hours

    duty_count = 0
    if emp.job_category in ("DOCTOR", "RESIDENT"):
        duty_count = (await session.execute(
            select(func.count(DutyAssignment.id))
            .join(DutySchedule, DutySchedule.id == DutyAssignment.schedule_id)
            .where(DutyAssignment.employee_id == emp.id,
                   DutyAssignment.shift == "N",
                   DutySchedule.yyyymm == ym))).scalar_one()

    # 무급휴가 일수 — 승인(APPROVED)된 UNPAID 휴가, 시작일의 월 귀속(단순화)
    y, m = int(ym[:4]), int(ym[5:7])
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    unpaid = float((await session.execute(
        select(func.coalesce(func.sum(LeaveRequest.days), 0))
        .where(LeaveRequest.employee_id == emp.id,
               LeaveRequest.status == "APPROVED",
               LeaveRequest.leave_type == "UNPAID",
               LeaveRequest.start_on >= date(y, m, 1),
               LeaveRequest.start_on < nxt))).scalar_one())

    return {"work_days": work_days, "night_hours": night_hours,
            "duty_count": duty_count, "ot_hours": round(ot_hours, 1),
            "holiday_hours": round(hol_within, 1),        # 휴일 8h 이내(×1.5) — H-2
            "holiday_ot_hours": round(hol_over, 1),       # 휴일 8h 초과(×2.0)
            "unpaid_days": unpaid,
            "work_hours": round(att.total_work_minutes / 60.0, 1) if att else 0.0}


async def resolve_monthly_inputs_bulk(session: AsyncSession, employees: list[Employee],
                                      yyyymm: str) -> dict[int, dict]:
    """전 직원 월 변동분 일괄 해소 — generate-bulk N+1 제거(점검 M1).

    개별 resolve_monthly_inputs와 동일 규칙 — 직원 수와 무관하게 쿼리 수 고정.
    """
    ym = _ym_dash(yyyymm)
    ids = [e.id for e in employees]
    if not ids:
        return {}

    att_by: dict[int, AttendanceSummary] = {
        a.employee_id: a for a in (await session.scalars(
            select(AttendanceSummary).where(AttendanceSummary.employee_id.in_(ids),
                                            AttendanceSummary.yyyymm == ym))).all()}
    # 휴일근로(H-2) — 월 스테이징 일괄 로드 후 사번별 분리(쿼리 2회 고정)
    y0, m0 = int(ym[:4]), int(ym[5:7])
    holidays = await _month_holidays(session, y0, m0)
    emp_nos = {e.employee_no for e in employees if e.employee_no}
    stg_by: dict[str, list] = {}
    if emp_nos:
        for r in (await session.scalars(
                select(GroupwareAttendanceStaging).where(
                    GroupwareAttendanceStaging.employee_no.in_(emp_nos),
                    GroupwareAttendanceStaging.work_on >= date(y0, m0, 1),
                    GroupwareAttendanceStaging.work_on
                    < date(y0 + (m0 == 12), (m0 % 12) + 1, 1)))).all():
            stg_by.setdefault(r.employee_no, []).append(r)
    duty_by: dict[int, int] = {
        emp_id: int(cnt) for emp_id, cnt in (await session.execute(
            select(DutyAssignment.employee_id, func.count(DutyAssignment.id))
            .join(DutySchedule, DutySchedule.id == DutyAssignment.schedule_id)
            .where(DutyAssignment.employee_id.in_(ids),
                   DutyAssignment.shift == "N", DutySchedule.yyyymm == ym)
            .group_by(DutyAssignment.employee_id))).all()}
    # 2026-10-09 전체 흐름 검토 C — 토요일(무급휴무일) 연장 판정용 듀티 배정일. 개별 경로엔 있는데
    # 일괄 경로가 _saturday_ot_split을 빠뜨려, 같은 직원이 생성 경로에 따라 토요일 근로 가산이
    # 달라졌다(일괄=토요일 근무를 평일 기준시간에 흡수 → 연장수당 과소). 쿼리 1회 추가로 동형화.
    sched_by: dict[int, set[date]] = {}
    for emp_id, d in (await session.execute(
            select(DutyAssignment.employee_id, DutyAssignment.duty_on).where(
                DutyAssignment.employee_id.in_(ids),
                DutyAssignment.duty_on >= date(y0, m0, 1),
                DutyAssignment.duty_on < date(y0 + (m0 == 12), (m0 % 12) + 1, 1)))).all():
        sched_by.setdefault(emp_id, set()).add(d)
    y, m = int(ym[:4]), int(ym[5:7])
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    unpaid_by: dict[int, float] = {
        emp_id: float(d) for emp_id, d in (await session.execute(
            select(LeaveRequest.employee_id,
                   func.coalesce(func.sum(LeaveRequest.days), 0))
            .where(LeaveRequest.employee_id.in_(ids),
                   LeaveRequest.status == "APPROVED",
                   LeaveRequest.leave_type == "UNPAID",
                   LeaveRequest.start_on >= date(y, m, 1),
                   LeaveRequest.start_on < nxt)
            .group_by(LeaveRequest.employee_id))).all()}

    out: dict[int, dict] = {}
    for emp in employees:
        att = att_by.get(emp.id)
        night = float(att.night_shift_count * NIGHT_SHIFT_HOURS) \
            if att and emp.job_category == "NURSE" else 0.0
        emp_stg = stg_by.get(emp.employee_no, [])
        hol_within, hol_over, hol_days = _holiday_split(emp_stg, holidays)
        sat_ot_hours, sat_ot_days = _saturday_ot_split(emp_stg, holidays,
                                                       sched_by.get(emp.id, set()))
        # 평일 연장 — 휴일·미배정 토요일 근무시간·근무일 제외 후 토요일 근로는 전부 연장 가산
        # (개별 resolve_monthly_inputs와 동일 식 — 2026-10-09 검토 C)
        ot = (max(0.0, att.total_work_minutes / 60.0 - (hol_within + hol_over) - sat_ot_hours
                  - DAILY_STANDARD_HOURS * max(0, att.work_days - hol_days - sat_ot_days))
              + sat_ot_hours) if att else 0.0
        out[emp.id] = {
            "work_days": att.work_days if att else 0,
            "night_hours": night,
            "duty_count": duty_by.get(emp.id, 0)
            if emp.job_category in ("DOCTOR", "RESIDENT") else 0,
            "ot_hours": round(ot, 1),
            "holiday_hours": round(hol_within, 1),
            "holiday_ot_hours": round(hol_over, 1),
            "unpaid_days": unpaid_by.get(emp.id, 0.0),
            "work_hours": round(att.total_work_minutes / 60.0, 1) if att else 0.0}
    return out
