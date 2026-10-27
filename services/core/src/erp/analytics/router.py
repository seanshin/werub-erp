"""경영분석 BI 라우터 (FR-19 확장) — 경영진 권한(DIRECTOR/ADMIN)."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from typing import Literal

from erp.analytics.audit_review_service import AuditReviewService
from erp.analytics.audit_service import AuditService
from erp.analytics.service import AnalyticsService
from erp.analytics.sim_scenario_service import SimScenarioService
from erp.audit.service import AuditService as _ChangeAuditService
from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session

router = APIRouter(prefix="/analytics", tags=["analytics"])
_svc = AnalyticsService()
_scn = SimScenarioService()
_audit = AuditService()
_audit_reviews = AuditReviewService()
_change_audit = _ChangeAuditService()   # 변경 감사(불변 audit_log) — 이상탐지기 _audit와 별개


def _require(user: CurrentUser):
    """단일 권한 게이트 위임 (M5 수렴)."""
    require_scope(user, "analytics.reports", "read")


@router.get("/overview")
async def overview(yyyymm: str, user: CurrentUser = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    _require(user)
    return await _svc.overview(session, yyyymm=yyyymm)


@router.get("/department-pl")
async def department_pl(yyyymm: str, user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user)
    return await _svc.department_pl(session, yyyymm=yyyymm)


@router.get("/sim-baseline")
async def sim_baseline(yyyymm: str, user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """재무 시뮬레이션 기준선 — 최근 6개월 월평균 손익·현금·AR/AP·부채/자본."""
    _require(user)
    return await _svc.sim_baseline(session, yyyymm=yyyymm)


@router.get("/segments")
async def segments(yyyymm: str, user: CurrentUser = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    """영업부문 보고(K-IFRS 1108) — 진료과별 수익·손익·자산·보고부문 임계."""
    _require(user)
    return await _svc.segment_report(session, yyyymm=yyyymm)


@router.get("/sim-dept-baseline")
async def sim_dept_baseline(yyyymm: str, user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """진료과별 시뮬레이션 기준선 — 최근 6개월 월평균 수익·원가·손익."""
    _require(user)
    return await _svc.sim_dept_baseline(session, yyyymm=yyyymm)


@router.get("/hr-turnover")
async def hr_turnover(yyyymm: str, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """이직률 분석 — 월별 입사/퇴직 추세 + 진료과별 이직률."""
    _require(user)
    return await _svc.hr_turnover(session, yyyymm=yyyymm)


@router.get("/department-pl/lines")
async def department_lines(yyyymm: str, dept_code: str,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """진료과 라인 드릴다운(2단계) — 해당 월·진료과 명세서별 청구·결정·삭감."""
    _require(user)
    return await _svc.department_lines(session, yyyymm=yyyymm, dept_code=dept_code)


@router.get("/trend")
async def trend(year: int, user: CurrentUser = Depends(get_current_user),
                session: AsyncSession = Depends(get_session)):
    _require(user)
    return await _svc.monthly_trend(session, year=year)


@router.get("/executive")
async def executive(yyyymm: str, user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """경영진 종합 대시보드 — 재무·인사·진료·보험·자금·재고·자산·정합 KPI(BI 마트 우선·라이브 폴백)."""
    _require(user)
    return await _svc.executive_cached(session, yyyymm=yyyymm)


@router.get("/financial-ratios")
async def financial_ratios(year: int, user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """재무비율(BI 마트 우선·라이브 폴백) — 대시보드 재무건전성 위젯·분석 화면용(성능검토 H-3).

    마감/공시용 라이브 원본은 `/finance/statements/financial-ratios`."""
    _require(user)
    return await _svc.financial_ratios_cached(session, year=year)


class DriversModel(BaseModel):
    """시뮬레이션 드라이버 — 무검증 JSON 저장 방지(범위·키 제약). 프론트 Drivers와 1:1."""
    model_config = ConfigDict(extra="forbid")
    growth: float = Field(ge=-1, le=5)        # 월 매출 성장률
    varRatio: float = Field(ge=0, le=1)       # 변동비율
    fixedDelta: float = Field(ge=-1, le=10)   # 고정비 증감
    taxRate: float = Field(ge=0, le=1)        # 법인세율
    capex: float = Field(ge=0, le=1e13)       # 투자(1월)
    loan: float = Field(ge=0, le=1e13)        # 차입(1월)
    seasonal: bool = False                     # 계절성 적용


class ScenarioBody(BaseModel):
    name: str
    drivers: DriversModel


@router.get("/sim-scenarios")
async def list_scenarios(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """내 재무 시뮬레이션 시나리오 목록(서버 저장)."""
    _require(user)
    return {"items": await _scn.list(session, user_id=user.user_id)}


@router.post("/sim-scenarios")
async def save_scenario(body: ScenarioBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """시나리오 저장(동일 이름 upsert) — 본인 소유."""
    _require(user)
    try:
        return await _scn.upsert(session, user_id=user.user_id,
                                 name=body.name, drivers=body.drivers.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/sim-scenarios/{sid}")
async def delete_scenario(sid: int, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """시나리오 삭제 — 본인 소유만."""
    _require(user)
    if not await _scn.delete(session, user_id=user.user_id, sid=sid):
        raise HTTPException(status_code=404, detail="없거나 권한 없음")
    return {"ok": True}


@router.get("/voucher")
async def voucher_status(date_from: date, date_to: date,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """검진권 운영 현황(집계) — 발급/사용/환급/정산 + 미사용 발급 잔액(선수금)."""
    _require(user)
    return await _svc.voucher_overview(session, date_from=date_from, date_to=date_to)


# ── 감사·이상탐지 (ADR D-7) — 전기완료 전표 기반, 리뷰 후보 제시 ──────────────
@router.get("/audit/summary")
async def audit_summary(date_from: date, date_to: date, threshold: float | None = None,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """감사 헤드라인 — Benford·중복전표·비정상분개 요약 카운트."""
    _require(user)
    return await _audit.summary(session, date_from=date_from, date_to=date_to,
                                threshold=threshold)


@router.get("/audit/benford")
async def audit_benford(date_from: date, date_to: date, side: str | None = "DEBIT",
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """Benford 법칙(첫자리 분포) — χ²·MAD 부합도로 조작·오류 신호 탐지."""
    _require(user)
    return await _audit.benford(session, date_from=date_from, date_to=date_to, side=side)


@router.get("/audit/duplicates")
async def audit_duplicates(date_from: date, date_to: date, window_days: int = 7,
                           side: str | None = None, unreviewed_only: bool = False,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """중복전표/중복지급 후보 — 동일 계정·금액·적요가 기간 내 2건 이상."""
    _require(user)
    return await _audit.duplicates(session, date_from=date_from, date_to=date_to,
                                   window_days=window_days, side=side,
                                   unreviewed_only=unreviewed_only)


@router.get("/audit/abnormal")
async def audit_abnormal(date_from: date, date_to: date, threshold: float | None = None,
                         unreviewed_only: bool = False,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """비정상 분개 후보 — 주말 기표·심야 입력·라운드넘버·승인한도 직전."""
    _require(user)
    return await _audit.abnormal_journals(session, date_from=date_from, date_to=date_to,
                                          threshold=threshold, unreviewed_only=unreviewed_only)


class AuditReviewBody(BaseModel):
    """감사 후보 검토완료 처리 — finding_key로 항목 식별, disposition 필수."""
    model_config = ConfigDict(extra="forbid")
    finding_type: Literal["ABNORMAL", "DUPLICATE"]
    finding_key: str = Field(min_length=1, max_length=80)
    disposition: Literal["NORMAL", "ACTION"]      # 정상(오탐) | 조치필요
    note: str | None = Field(default=None, max_length=2000)


@router.post("/audit/reviews")
async def audit_review_upsert(body: AuditReviewBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """후보 검토완료 처리(동일 finding 재처리 시 갱신) — reviewed_by=현재 사용자."""
    _require(user)
    try:
        r = await _audit_reviews.upsert(
            session, finding_type=body.finding_type, finding_key=body.finding_key,
            disposition=body.disposition, note=body.note, actor=user.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # 사인오프를 불변 audit_log에 기록(G4) — 누가·무엇을·어떤 판정으로 종결했는지 tamper-evident.
    await _change_audit.record_audit(
        session, actor=user.user_id, action="AUDIT_REVIEW_SIGNOFF",
        entity="finance.audit_reviews", target_id=str(r.id),
        after={"finding_type": body.finding_type, "finding_key": body.finding_key,
               "disposition": body.disposition, "note": body.note})
    return {"id": r.id, "status": r.status, "disposition": r.disposition}


@router.delete("/audit/reviews/{review_id}")
async def audit_review_delete(review_id: int,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """검토 취소 — 후보가 다시 미검토로 돌아감."""
    _require(user)
    if not await _audit_reviews.delete(session, review_id=review_id):
        raise HTTPException(status_code=404, detail="검토 기록이 없습니다")
    # 사인오프 취소(재개)도 불변 audit_log에 기록(G4) — 종결이 되돌려진 사실을 추적.
    await _change_audit.record_audit(
        session, actor=user.user_id, action="AUDIT_REVIEW_REVOKE",
        entity="finance.audit_reviews", target_id=str(review_id))
    return {"ok": True}
