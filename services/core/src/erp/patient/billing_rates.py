"""기관 종별 본인부담률 로더 (D-6 정밀화) — 진료일 시점 유효 종별 주입.

InstitutionGradeSetting(시점관리) → split_line(rates=..., grade=...).
미설정 시 코드 기본값(병원 40%)으로 안전 폴백.
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.regulatory.models import InstitutionGradeSetting


async def load_billing_rates(session: AsyncSession, as_of: date) -> dict:
    """진료일(as_of) 시점 유효한 기관 종별 설정 → rates + grade.

    반환: {"grade": str, "rates": {OUTPATIENT, INPATIENT, MEAL, SPECIAL_CASE}}.
    미설정이면 grade=None·빈 rates(엔진 DEFAULT_RATES = 병원 40% 사용).
    """
    row = await session.scalar(
        select(InstitutionGradeSetting)
        .where(InstitutionGradeSetting.effective_from <= as_of)
        .order_by(InstitutionGradeSetting.effective_from.desc())
        .limit(1))
    if row is None:
        return {"grade": None, "rates": {}}
    return {"grade": row.grade,
            "rates": {"OUTPATIENT": float(row.outpatient_rate),
                      "INPATIENT": float(row.inpatient_rate),
                      "MEAL": float(row.meal_rate),
                      "SPECIAL_CASE": float(row.special_case_rate)}}


async def current_grade(session: AsyncSession) -> dict | None:
    """현재 유효 기관 종별 (관리 화면 표시용)."""
    from datetime import date as _date
    out = await load_billing_rates(session, _date.today())
    return None if out["grade"] is None else out
