"""듀티 편성 (FR-06) + 노동규제 위반 경고 (FR-32).

시프트당 시간·야간/근무일 플래그는 hr.shift_types 카탈로그, 임계값은 regulatory.labor_rules
에서 조회(둘 다 인사팀이 관리). 빈 표면 아래 코드 기본값으로 폴백 → 하위호환(무영향).
경고는 저장을 막지 않음(경고 표시만 — Plan FR-32).

전공의 80h 룰 실효화: 과거엔 SHIFT_HOURS가 D/E/N 모두 8h 고정이라 주당 최대 56h →
`resident_avg_4week`(80h)가 절대 미발화하는 죽은 룰이었음. 이제 카탈로그에 당직/온콜 등
장시간 시프트(예 'OC' 24h)를 등록하면 80h 룰이 실제로 발화한다(전공의법 §7① 보호 실효화).
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.months import require_ym
from erp.common.kst import kst_iso
from erp.errors import ValidationError
from erp.hr.models import DutyAssignment, DutySchedule, Employee, ShiftType
from erp.regulatory.models import LaborRule

# 코드 기본값(폴백) — DB(shift_types/labor_rules) 미적재 시에만 사용. 적재되면 DB가 SoT.
SHIFT_HOURS = {"D": 8, "E": 8, "N": 8, "OFF": 0}
_NIGHT_SHIFTS = {"N"}
_WORKDAY_SHIFTS = {"D", "E", "N"}  # OFF 제외
DEFAULT_RULES = {
    "weekly_hours_general": 52,     # 근로기준법 §50+§53: 40h + 연장 12h (달력 주 단위)
    "resident_avg_4week": 80,       # 전공의법 §7①: 4주 평균 주 80h
    "resident_week_max": 88,        # 전공의법 §7①: 1주 최대 88h (평균과 별도 단일주 상한)
    "max_consecutive_nights": 3,    # 연속 야간 제한 (권고)
    "max_consecutive_workdays": 6,  # 연속 근무일 (주휴 — 근로기준법 §55 취지)
}


@dataclass
class ShiftCatalog:
    """시프트 코드 → 시간·야간/근무일 플래그 (DB 카탈로그 + 코드 폴백 병합)."""
    hours: dict[str, float]
    night: set[str]
    workday: set[str]

    def __contains__(self, code: str) -> bool:
        return code in self.hours


@dataclass
class DutyCell:
    employee_id: int
    duty_on: date
    shift: str  # D | E | N | OFF


@dataclass
class Violation:
    rule: str
    employee_id: int
    detail: str


class DutyService:
    async def _catalog(self, session: AsyncSession) -> ShiftCatalog:
        """시프트 카탈로그 로드 — 코드 기본값 위에 hr.shift_types(active) 오버레이."""
        cat = ShiftCatalog(hours=dict(SHIFT_HOURS), night=set(_NIGHT_SHIFTS),
                           workday=set(_WORKDAY_SHIFTS))
        rows = (await session.execute(
            select(ShiftType).where(ShiftType.active.is_(True)))).scalars().all()
        for r in rows:
            cat.hours[r.code] = float(r.work_hours)
            (cat.night.add if r.is_night else cat.night.discard)(r.code)
            (cat.workday.add if r.counts_as_workday else cat.workday.discard)(r.code)
        return cat

    async def _thresholds(self, session: AsyncSession) -> dict[str, float]:
        """임계값 로드 — DEFAULT_RULES 위에 regulatory.labor_rules(active) 오버레이."""
        rules = dict(DEFAULT_RULES)
        rows = (await session.execute(
            select(LaborRule).where(LaborRule.active.is_(True)))).scalars().all()
        for r in rows:
            rules[r.rule_key] = float(r.threshold)
        return rules

    async def load_month(self, session: AsyncSession, *, department_id: int,
                         yyyymm: str) -> dict:
        """저장된 월 근무표 되읽기 — 편성 화면 재진입 시 기존 배정을 복원한다.

        조회 EP가 없어 화면이 **항상 빈 그리드로 시작**했고, `save_month`가 delete-then-insert라
        그 상태로 저장하면 **기존 배정이 전량 삭제**됐다(데이터 손실 경로).
        """
        schedule = (await session.execute(
            select(DutySchedule).where(DutySchedule.department_id == department_id,
                                       DutySchedule.yyyymm == yyyymm))).scalar_one_or_none()
        if schedule is None:
            return {"schedule_id": None, "status": None, "published_at": None, "cells": []}
        rows = (await session.scalars(
            select(DutyAssignment).where(DutyAssignment.schedule_id == schedule.id)
            .order_by(DutyAssignment.duty_on, DutyAssignment.employee_id))).all()
        return {"schedule_id": schedule.id, "status": schedule.status,
                "published_at": kst_iso(schedule.published_at),
                "cells": [{"employee_id": r.employee_id, "duty_on": r.duty_on.isoformat(),
                           "shift": r.shift} for r in rows]}

    async def save_month(self, session: AsyncSession, *, department_id: int, yyyymm: str,
                         cells: list[DutyCell]) -> tuple[DutySchedule, list[Violation]]:
        """월 듀티 저장(덮어쓰기) + 룰 평가 — 경고 반환 (저장은 진행, FR-32)."""
        yyyymm = require_ym(yyyymm)
        catalog = await self._catalog(session)
        for c in cells:
            if c.shift not in catalog:
                raise ValidationError(f"잘못된 근무 코드: {c.shift}")

        schedule = (await session.execute(
            select(DutySchedule).where(DutySchedule.department_id == department_id,
                                       DutySchedule.yyyymm == yyyymm)
        )).scalar_one_or_none()
        if schedule is None:
            schedule = DutySchedule(department_id=department_id, yyyymm=yyyymm)
            session.add(schedule)
            await session.flush()
        else:
            await session.execute(
                delete(DutyAssignment).where(DutyAssignment.schedule_id == schedule.id))

        for c in cells:
            session.add(DutyAssignment(schedule_id=schedule.id, employee_id=c.employee_id,
                                       duty_on=c.duty_on, shift=c.shift))

        violations = await self._evaluate(session, cells, catalog)
        return schedule, violations

    async def publish(self, session: AsyncSession, *, schedule_id: int) -> DutySchedule:
        schedule = (await session.execute(
            select(DutySchedule).where(DutySchedule.id == schedule_id))).scalar_one_or_none()
        if schedule is None:
            raise ValidationError("존재하지 않는 듀티표입니다")
        schedule.status = "PUBLISHED"
        schedule.published_at = datetime.now(timezone.utc)
        return schedule

    # ── 노동규제 룰 평가 (FR-32) ─────────────────────────────
    async def _evaluate(self, session: AsyncSession, cells: list[DutyCell],
                        catalog: ShiftCatalog) -> list[Violation]:
        violations: list[Violation] = []
        by_emp: dict[int, list[DutyCell]] = {}
        for c in cells:
            by_emp.setdefault(c.employee_id, []).append(c)

        emp_ids = list(by_emp.keys())
        groups = {e.id: e.job_group for e in (await session.execute(
            select(Employee).where(Employee.id.in_(emp_ids)))).scalars().all()}
        rules = await self._thresholds(session)

        for emp_id, emp_cells in by_emp.items():
            emp_cells.sort(key=lambda c: c.duty_on)
            is_resident = groups.get(emp_id) == "RESIDENT"

            # 주(월~일) 단위 시간 합산 + 주별 편성일수 — 카탈로그 시간 사용(당직 등 장시간 반영)
            weekly: dict[tuple[int, int], float] = {}
            weekly_days: dict[tuple[int, int], set[date]] = {}
            for c in emp_cells:
                iso = c.duty_on.isocalendar()
                key = (iso[0], iso[1])
                weekly[key] = weekly.get(key, 0) + catalog.hours[c.shift]
                weekly_days.setdefault(key, set()).add(c.duty_on)

            if is_resident:
                # 전공의법 §7① — 4주 평균 80h. 평균 분모는 **완전주(7일 편성)만** —
                # 월경계 부분주(1~6일)가 분모를 키워 평균을 희석/위반을 가리는 것 방지.
                limit = rules["resident_avg_4week"]
                full = [h for k, h in weekly.items() if len(weekly_days[k]) == 7]
                if full:
                    avg = sum(full) / len(full)
                    if avg > limit:
                        violations.append(Violation(
                            rule="RESIDENT_AVG_HOURS", employee_id=emp_id,
                            detail=f"완전주 평균 {avg:.1f}h > {limit:.0f}h "
                                   f"({len(full)}주, 전공의법 §7① — 4주 평균)"))
                # 단일주 절대상한 — 평균이 통과해도 한 주 스파이크(부분주 포함)는 별도 적발
                wmax = rules.get("resident_week_max", DEFAULT_RULES["resident_week_max"])
                for (year, week), hours in weekly.items():
                    if hours > wmax:
                        violations.append(Violation(
                            rule="RESIDENT_WEEK_MAX", employee_id=emp_id,
                            detail=f"{year}-W{week}: {hours:.0f}h > {wmax:.0f}h "
                                   f"(전공의법 §7① — 1주 최대)"))
            else:
                limit = rules["weekly_hours_general"]
                for (year, week), hours in weekly.items():
                    if hours > limit:
                        violations.append(Violation(
                            rule="WEEKLY_HOURS", employee_id=emp_id,
                            detail=f"{year}-W{week}: {hours:.0f}h > {limit:.0f}h (근로기준법 §50·§53)"))

            # 연속 야간·연속 근무 — 카탈로그 플래그(is_night·counts_as_workday) 기준
            consec_n = consec_work = 0
            prev_day: date | None = None
            for c in emp_cells:
                contiguous = prev_day is not None and (c.duty_on - prev_day).days == 1
                is_night = c.shift in catalog.night
                worked = c.shift in catalog.workday
                consec_n = (consec_n + 1 if is_night and (contiguous or prev_day is None)
                            else (1 if is_night else 0))
                consec_work = (consec_work + 1 if worked and (contiguous or prev_day is None)
                               else (1 if worked else 0))
                if consec_n > rules["max_consecutive_nights"]:
                    violations.append(Violation(
                        rule="CONSECUTIVE_NIGHTS", employee_id=emp_id,
                        detail=f"{c.duty_on}: 연속 야간 {consec_n}일"))
                if consec_work > rules["max_consecutive_workdays"]:
                    violations.append(Violation(
                        rule="CONSECUTIVE_WORKDAYS", employee_id=emp_id,
                        detail=f"{c.duty_on}: 연속 근무 {consec_work}일"))
                prev_day = c.duty_on
        return violations
