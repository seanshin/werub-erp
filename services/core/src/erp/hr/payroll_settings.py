"""급여 정책 설정 서비스 — 성과급 원천징수 방식 등(싱글턴 id=1).

세무 정책(성과급 원천징수)은 하드코딩하지 않고 기관 관리자가 설정. 기본 NONE(현행·연말정산 정산).
소비처: incentive_service(성과급 승인 시 원천징수), 관리화면(/admin/payroll-settings).
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.hr.models import PayrollSettings

_METHODS = ("NONE", "FLAT", "COMBINED")
_NIGHT_MODES = ("FLAT", "STATUTORY", "MAX")


class PayrollSettingsService:
    async def get(self, session: AsyncSession) -> PayrollSettings | None:
        return await session.scalar(select(PayrollSettings).where(PayrollSettings.id == 1))

    async def effective(self, session: AsyncSession) -> dict:
        """적용 설정 — 미설정 시 안전 기본값(NONE·현행 동작·사업주부담 OFF)."""
        row = await self.get(session)
        return {"incentive_withholding_method": (row.incentive_withholding_method if row else "NONE"),
                "incentive_withholding_rate": (float(row.incentive_withholding_rate) if row else 0.0),
                "combined_months": (int(row.combined_months) if row else 1),
                "employer_burden_posting": (bool(row.employer_burden_posting) if row else False),
                "employer_ei_rate": (float(row.employer_ei_rate) if row else 0.0115),
                "employer_wc_rate": (float(row.employer_wc_rate) if row else 0.007),
                "night_pay_mode": (row.night_pay_mode if row else "FLAT"),
                "incentive_grade_link": (bool(row.incentive_grade_link) if row else False)}

    async def upsert(self, session: AsyncSession, *, actor: int, incentive_withholding_method: str,
                     incentive_withholding_rate: float = 0.0, combined_months: int = 1,
                     employer_burden_posting: bool = False, employer_ei_rate: float = 0.0115,
                     employer_wc_rate: float = 0.007,
                     night_pay_mode: str = "FLAT",
                     incentive_grade_link: bool = False) -> PayrollSettings:
        method = (incentive_withholding_method or "NONE").upper()
        if method not in _METHODS:
            raise ValidationError(f"원천징수 방식은 {_METHODS} 중 하나여야 합니다")
        rate = float(incentive_withholding_rate or 0)
        if method == "FLAT" and not (0 < rate < 1):
            raise ValidationError("FLAT 정률은 0 초과 1 미만이어야 합니다 (예: 0.033)")
        months = int(combined_months or 1)
        if method == "COMBINED" and not (1 <= months <= 12):
            raise ValidationError("상여 지급대상기간은 1~12개월이어야 합니다")
        for name, v in (("고용보험 사업주", employer_ei_rate), ("산재", employer_wc_rate)):
            if not (0 <= float(v) < 1):
                raise ValidationError(f"{name} 요율은 0 이상 1 미만이어야 합니다")
        row = await self.get(session)
        if row is None:
            row = PayrollSettings(id=1)
            session.add(row)
        row.incentive_withholding_method = method
        row.incentive_withholding_rate = rate if method == "FLAT" else 0
        row.combined_months = months if method == "COMBINED" else 1
        row.employer_burden_posting = bool(employer_burden_posting)
        row.employer_ei_rate = float(employer_ei_rate)
        row.employer_wc_rate = float(employer_wc_rate)
        nmode = (night_pay_mode or "FLAT").upper()
        if nmode not in _NIGHT_MODES:
            raise ValidationError(f"야간수당 방식은 {_NIGHT_MODES} 중 하나여야 합니다")
        row.night_pay_mode = nmode
        row.incentive_grade_link = bool(incentive_grade_link)
        row.updated_by = actor
        await session.flush()
        return row
