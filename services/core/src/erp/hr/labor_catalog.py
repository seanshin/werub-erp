"""노동규제 룰 관리 (FR-32) — 시프트 카탈로그·임계값 조회/수정 서비스.

duty_service 엔진이 SoT로 쓰는 hr.shift_types·regulatory.labor_rules를 인사팀이
화면에서 보고 관리하기 위한 로더/업서트. 조회는 코드 기본값(폴백) 위에 DB 행을
오버레이한 **유효값 병합 뷰**(source/overridden 태깅)를 돌려준다 — duty_service의
_catalog/_thresholds 병합 규칙과 동일. 빈 표면 코드 기본값이 그대로 유효(하위호환).
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.hr.duty_service import (
    DEFAULT_RULES,
    SHIFT_HOURS,
    _NIGHT_SHIFTS,
    _WORKDAY_SHIFTS,
)
from erp.hr.models import ShiftType
from erp.regulatory.models import LaborRule

_audit = AuditService()

# 코드 기본 시프트 라벨(폴백 표시용). DB 등록 시 등록 라벨이 우선.
_SHIFT_LABELS = {"D": "주간", "E": "저녁", "N": "야간", "OFF": "휴무"}

# 임계값 키 → (표시명, 기본 근거 조항). DB 행이 notice_ref를 가지면 그쪽이 우선.
_RULE_META = {
    "weekly_hours_general": ("일반직 주간 근로시간", "근로기준법 §50+§53 (40+12h)"),
    "resident_avg_4week": ("전공의 4주 평균 주", "전공의법 §7①"),
    "resident_week_max": ("전공의 1주 최대", "전공의법 §7①"),
    "max_consecutive_nights": ("연속 야간 제한", "권고"),
    "max_consecutive_workdays": ("연속 근무일 제한", "근로기준법 §55 취지"),
}


# ── 시프트 카탈로그 ──────────────────────────────────────────
def _fmt_time(t) -> str | None:
    return t.strftime("%H:%M") if t is not None else None


def _parse_time(s):
    """'HH:MM' → time, 빈값/None → None."""
    from datetime import datetime as _dt
    if s in (None, ""):
        return None
    try:
        return _dt.strptime(s.strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        raise ValidationError("시각 형식은 HH:MM 이어야 합니다") from None


def _default_times(code: str) -> tuple[str | None, str | None]:
    """코드 기본 시프트(D/E/N)의 근태판정 기준 시각 — attendance SoT 재사용(드리프트 방지)."""
    from erp.hr.attendance import SHIFT_TIMES
    t = SHIFT_TIMES.get(code)
    return (_fmt_time(t[0]), _fmt_time(t[1])) if t else (None, None)


async def list_shift_types(session: AsyncSession) -> list[dict]:
    """유효 시프트 카탈로그 — 코드 기본값(D/E/N/OFF) 위에 hr.shift_types(active) 오버레이.

    엔진(duty_service._catalog)이 active 행만 적용하므로 여기서도 active만 오버레이해
    화면의 '유효값'이 실제 룰 평가값과 일치하게 한다(비활성 행은 코드 기본값으로 폴백).
    """
    db = {r.code: r for r in (await session.scalars(
        select(ShiftType).where(ShiftType.active.is_(True)))).all()}
    out: list[dict] = []
    for code in SHIFT_HOURS:
        row = db.pop(code, None)
        if row is not None:
            ds, de = _default_times(code)
            out.append({"code": code, "label": row.label,
                        "work_hours": float(row.work_hours), "is_night": row.is_night,
                        "counts_as_workday": row.counts_as_workday, "active": row.active,
                        "start_time": _fmt_time(row.start_time) or ds,
                        "end_time": _fmt_time(row.end_time) or de,
                        "source": "DB", "overridden": True})
        else:
            ds, de = _default_times(code)
            out.append({"code": code, "label": _SHIFT_LABELS.get(code, code),
                        "work_hours": float(SHIFT_HOURS[code]),
                        "is_night": code in _NIGHT_SHIFTS,
                        "counts_as_workday": code in _WORKDAY_SHIFTS,
                        "start_time": ds, "end_time": de,
                        "active": True, "source": "기본값", "overridden": False})
    # 코드 기본에 없는 운영 추가 시프트(당직 OC 등)
    out += [{"code": r.code, "label": r.label, "work_hours": float(r.work_hours),
             "is_night": r.is_night, "counts_as_workday": r.counts_as_workday,
             "start_time": _fmt_time(r.start_time), "end_time": _fmt_time(r.end_time),
             "active": r.active, "source": "DB", "overridden": True}
            for r in db.values()]
    return out


async def upsert_shift_type(session: AsyncSession, *, actor: int, code: str, label: str,
                            work_hours: float, is_night: bool, counts_as_workday: bool,
                            active: bool = True, start_time=None, end_time=None) -> ShiftType:
    code = code.strip().upper()
    if not code:
        raise ValidationError("시프트 코드는 비어있을 수 없습니다")
    if len(code) > 4:
        raise ValidationError("시프트 코드는 4자 이하여야 합니다")
    if work_hours < 0 or work_hours > 24:
        raise ValidationError("근무시간은 0~24 사이여야 합니다")
    if not label.strip():
        raise ValidationError("시프트 명칭은 비어있을 수 없습니다")
    st, et = _parse_time(start_time), _parse_time(end_time)
    row = await session.scalar(select(ShiftType).where(ShiftType.code == code))
    before = ({"work_hours": float(row.work_hours), "is_night": row.is_night,
               "counts_as_workday": row.counts_as_workday, "active": row.active}
              if row else None)
    if row is None:
        row = ShiftType(code=code, label=label, work_hours=work_hours, is_night=is_night,
                        counts_as_workday=counts_as_workday, active=active,
                        start_time=st, end_time=et)
        session.add(row)
        await session.flush()
    else:
        row.label, row.work_hours = label, work_hours
        row.is_night, row.counts_as_workday, row.active = is_night, counts_as_workday, active
        row.start_time, row.end_time = st, et
    after = {"work_hours": work_hours, "is_night": is_night,
             "counts_as_workday": counts_as_workday, "active": active}
    await _audit.record_audit(session, actor=actor, action="UPDATE" if before else "CREATE",
                              entity="hr.shift_types", target_id=code,
                              before=before, after=after)
    return row


# ── 노동규제 임계값 ──────────────────────────────────────────
async def list_labor_rules(session: AsyncSession) -> list[dict]:
    """유효 임계값 — DEFAULT_RULES(법 조문 초안) 위에 regulatory.labor_rules(active) 오버레이.

    엔진(duty_service._thresholds)이 active 행만 적용 → 여기서도 active만 오버레이.
    """
    db = {r.rule_key: r for r in (await session.scalars(
        select(LaborRule).where(LaborRule.active.is_(True)))).all()}
    out: list[dict] = []
    for key, default in DEFAULT_RULES.items():
        label, default_notice = _RULE_META.get(key, (key, None))
        row = db.pop(key, None)
        if row is not None:
            out.append({"rule_key": key, "label": label,
                        "threshold": float(row.threshold),
                        "notice_ref": row.notice_ref or default_notice,
                        "default": float(default), "active": row.active,
                        "source": "DB", "overridden": True})
        else:
            out.append({"rule_key": key, "label": label, "threshold": float(default),
                        "notice_ref": default_notice, "default": float(default),
                        "active": True, "source": "기본값", "overridden": False})
    # 코드 기본에 없는 운영 추가 임계값
    out += [{"rule_key": r.rule_key, "label": _RULE_META.get(r.rule_key, (r.rule_key,))[0],
             "threshold": float(r.threshold), "notice_ref": r.notice_ref,
             "default": None, "active": r.active, "source": "DB", "overridden": True}
            for r in db.values()]
    return out


async def upsert_labor_rule(session: AsyncSession, *, actor: int, rule_key: str,
                            threshold: float, notice_ref: str | None = None,
                            active: bool = True) -> LaborRule:
    rule_key = rule_key.strip()
    if not rule_key:
        raise ValidationError("룰 키는 비어있을 수 없습니다")
    if threshold <= 0:
        raise ValidationError("임계값은 0보다 커야 합니다")
    row = await session.scalar(select(LaborRule).where(LaborRule.rule_key == rule_key))
    before = ({"threshold": float(row.threshold), "notice_ref": row.notice_ref,
               "active": row.active} if row else None)
    if row is None:
        row = LaborRule(rule_key=rule_key, threshold=threshold,
                        notice_ref=notice_ref, active=active)
        session.add(row)
        await session.flush()
    else:
        row.threshold, row.active = threshold, active
        if notice_ref is not None:
            row.notice_ref = notice_ref
    await _audit.record_audit(session, actor=actor, action="UPDATE" if before else "CREATE",
                              entity="regulatory.labor_rules", target_id=rule_key,
                              before=before,
                              after={"threshold": threshold, "notice_ref": notice_ref,
                                     "active": active})
    return row
