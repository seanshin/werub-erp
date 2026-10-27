"""원가(ABC) 라우터 (FR-50) — 재무 권한(DIRECTOR/ADMIN). design §4.3."""
from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.months import YearMonth
from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.costing.abc_service import AbcAllocationService
from erp.costing.service import CostingService
from erp.db import get_session

router = APIRouter(prefix="/costing", tags=["costing"])
_svc = CostingService()
_abc = AbcAllocationService()


def _require_finance(user: CurrentUser):
    """단일 권한 게이트 위임 (M5 수렴)."""
    require_scope(user, "finance.vouchers", "read")


@router.get("/summary")
async def cost_summary(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    _require_finance(user)
    return await _svc.summary(session, yyyymm=yyyymm)


@router.get("/preauth-summary")
async def preauth_summary(since: date, until: date,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """사전승인 고가오더 카테고리별 집계(category_summary·관리회계) — HIS e결재 연계(GL 미전기·약정).

    응급우회 미결(EMERGENCY_BYPASS·post_approved=False)을 별도 집계 — 사후 통제 유도(규제·감사)."""
    from erp.finance.models import PreauthOrder
    _require_finance(user)
    window = (PreauthOrder.approved_on >= since, PreauthOrder.approved_on <= until)
    rows = (await session.execute(
        select(PreauthOrder.category, func.count(), func.coalesce(func.sum(PreauthOrder.amount), 0))
        .where(*window)
        .group_by(PreauthOrder.category)
        .order_by(func.sum(PreauthOrder.amount).desc()))).all()
    items = [{"category": c, "count": int(n), "amount": float(a)} for c, n, a in rows]
    # 사후승인 미결(응급우회 사전승인 없이 발행·아직 사후승인 안 됨) — 미결 건수·유형별
    pending = (await session.execute(
        select(PreauthOrder.order_type, func.count(),
               func.coalesce(func.sum(PreauthOrder.amount), 0))
        .where(*window, PreauthOrder.channel == "EMERGENCY_BYPASS",
               PreauthOrder.post_approved.is_(False))
        .group_by(PreauthOrder.order_type))).all()
    pending_items = [{"order_type": t, "count": int(n), "amount": float(a)} for t, n, a in pending]
    return {"since": str(since), "until": str(until),
            "total_amount": sum(i["amount"] for i in items),
            "total_count": sum(i["count"] for i in items), "by_category": items,
            "post_approval_pending_count": sum(i["count"] for i in pending_items),
            "post_approval_pending_amount": sum(i["amount"] for i in pending_items),
            "post_approval_pending_by_type": pending_items}


@router.get("/by-department")
async def cost_by_department(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """진료과별 인건비 + 삭감손실(원가 차원). 인건비=급여, 삭감=청구(care_month=yyyymm)."""
    _require_finance(user)
    return {
        "yyyymm": yyyymm,
        "labor_by_department": await _svc.labor_by_department(session, yyyymm=yyyymm),
        "deduction_loss_by_department": await _svc.deduction_loss_by_department(
            session, care_month=yyyymm),
    }


@router.get("/abc")
async def abc_allocate(yyyymm: YearMonth, material_amount: float = 0, overhead_amount: float = 0,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """ABC 원가 배부 (FR-42) — 인건비(자동)·재료/간접(입력) 풀을 진료과에 동인 배부."""
    _require_finance(user)
    return await _abc.allocate(session, yyyymm=yyyymm, material_amount=material_amount,
                               overhead_amount=overhead_amount)


class DriverValueBody(BaseModel):
    period: YearMonth
    department_id: int
    driver: str          # CASES | AREA | USAGE
    value: float


@router.get("/driver-values")
async def list_driver_values(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """ABC 원가동인 값 조회 (FR-42 — D42 협의값 입력 기반 정밀 배부)."""
    from erp.finance.models import CostDriverValue
    _require_finance(user)
    rows = (await session.execute(
        select(CostDriverValue).where(CostDriverValue.period == yyyymm))).scalars().all()
    return {"items": [{"id": r.id, "department_id": r.department_id, "driver": r.driver,
                       "value": float(r.value)} for r in rows]}


@router.put("/driver-values")
async def upsert_driver_value(body: DriverValueBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """원가동인 값 upsert — 입력 시 ABC 배부가 인원수 폴백 대신 정밀 배부."""
    from erp.finance.models import CostDriverValue
    _require_finance(user)
    existing = (await session.execute(
        select(CostDriverValue).where(
            CostDriverValue.period == body.period,
            CostDriverValue.department_id == body.department_id,
            CostDriverValue.driver == body.driver))).scalar_one_or_none()
    if existing:
        existing.value = body.value
        return {"id": existing.id, "updated": True}
    dv = CostDriverValue(period=body.period, department_id=body.department_id,
                         driver=body.driver, value=body.value)
    session.add(dv)
    await session.flush()
    return {"id": dv.id, "updated": False}


# ── 원가 배부 스냅샷·진료과 손익 (FR-50 확장) ──────────────────
_snap = None


def _snap_service():
    global _snap
    if _snap is None:
        from erp.costing.service import CostSnapshotService
        _snap = CostSnapshotService()
    return _snap


class SnapshotBody(BaseModel):
    yyyymm: YearMonth
    material_amount: float = 0
    overhead_amount: float = 0


@router.post("/snapshot")
async def save_snapshot(body: SnapshotBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """원가 배부 스냅샷 저장(마감 기준) — 부서별 배부원가 확정·정합 검증."""
    _require_finance(user)
    out = await _snap_service().save_snapshot(
        session, yyyymm=body.yyyymm, material_amount=body.material_amount,
        overhead_amount=body.overhead_amount, user_id=user.user_id)
    await session.commit()
    return out


@router.get("/snapshot")
async def get_snapshot(yyyymm: YearMonth,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """저장된 배부 스냅샷 — 부서별 인건비·재료비·간접비·총원가."""
    _require_finance(user)
    return {"yyyymm": yyyymm, "items": await _snap_service().snapshots(session, yyyymm=yyyymm)}


@router.get("/department-pl")
async def department_pl(yyyymm: YearMonth,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """진료과별 손익 — 보험 결정수익 − 배부원가 − 삭감손실(스냅샷 선저장 권장)."""
    _require_finance(user)
    return {"yyyymm": yyyymm, "items": await _snap_service().department_pl(session, yyyymm=yyyymm)}


@router.get("/multistage")
async def multistage_allocation(yyyymm: YearMonth,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """2단계 원가배부 — 지원부서 원가를 진료부서에 인원수 배부(스냅샷 기준)."""
    _require_finance(user)
    return await _snap_service().multistage(session, yyyymm=yyyymm)
