"""월마감 일괄(month-end close) 엔드포인트 — 크로스도메인 오케스트레이션(finance+hr).

감가(finance)+퇴직충당(hr)+리스(finance)를 묶으므로 도메인이 아닌 app 레이어에 둔다
(hr↔finance 순환 회피). URL은 /finance/month-end로 보존(웹 호출 계약 유지).
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from erp.app.month_end_service import MonthEndService
from erp.common.months import YearMonth
from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session

router = APIRouter(prefix="/finance", tags=["month-end"])


@router.post("/month-end/{yyyymm}/run")
async def run_month_end(yyyymm: YearMonth,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """월마감 일괄 — 감가상각·퇴직급여충당·리스 회계 실행(멱등)."""
    require_scope(user, "finance.periods", "write")
    out = await MonthEndService().run(session, yyyymm=yyyymm, user_id=user.user_id)
    await session.commit()
    return out
