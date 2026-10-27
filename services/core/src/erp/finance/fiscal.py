"""회계연도(fiscal year) 범위 해석 — 기관 설정(org_settings.fiscal_year_start_month) 기반.

FY N = N년 시작월 1일 ~ +12개월 − 1일 (시작월 1=역년 N/1/1~N/12/31 — 현행 무회귀 기본).
적용: 재무제표(연간 비율)·연말결산·공시/주석·이연법인세. **비적용(법정 달력연도)**: 부가세·
세금계산서 과세기간, 연말정산·4대보험·간이세액(소득세법), 컴플라이언스 법정기한.
"""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def fiscal_range_for(start_month: int, fiscal_year: int) -> tuple[date, date]:
    """순수 계산 — FY N = (N년 start_month 1일, +12개월 −1일). 말일·윤년 안전(월초 기준)."""
    start = date(fiscal_year, start_month, 1)
    end = date(fiscal_year + 1, start_month, 1) - timedelta(days=1)
    return start, end


async def fiscal_start_month(session: AsyncSession) -> int:
    """기관 회계연도 시작월(미설정 시 1=역년)."""
    from erp.common.models import OrgSettings
    m = await session.scalar(select(OrgSettings.fiscal_year_start_month)
                             .where(OrgSettings.id == 1))
    return int(m) if m else 1


async def fiscal_range(session: AsyncSession, fiscal_year: int) -> tuple[date, date]:
    """기관 설정 기반 FY 범위."""
    return fiscal_range_for(await fiscal_start_month(session), fiscal_year)
