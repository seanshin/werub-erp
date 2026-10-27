"""컴플라이언스 만기·한도 통합 엔드포인트 — 크로스도메인 집계(app 레이어).

면허·법정교육/검진·4대보험 신고·방사선 장비검사·재고 유효기간 만기 + 방사선 피폭·환자안전 미검토를
하나의 운영 뷰로. 경영·관리 역할 전용(regulatory.compliance read).
"""
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from erp.app.compliance_service import ComplianceDeadlineService
from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session

router = APIRouter(prefix="/compliance", tags=["compliance"])


@router.get("/deadlines")
async def compliance_deadlines(as_of: date | None = None, horizon_days: int = 90,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """만기 임박·경과 통합 — 긴급도(overdue/D-7/D-30/D-90)·카테고리 요약 + 한도/미검토 경보."""
    require_scope(user, "regulatory.compliance", "read")
    horizon = max(1, min(horizon_days, 365))
    return await ComplianceDeadlineService().deadlines(
        session, as_of=as_of or date.today(), horizon_days=horizon)
