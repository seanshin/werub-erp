"""세무 신고 라우터 (FR-07 확장) — 재무/인사 권한."""
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.months import YearMonth
from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.tax.service import TaxService

router = APIRouter(prefix="/tax", tags=["tax"])
_svc = TaxService()


def _require(user: CurrentUser):
    """단일 권한 게이트 위임 (M5 수렴)."""
    require_scope(user, "finance.statements", "read")


@router.get("/withholding")
async def withholding(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user)
    return await _svc.withholding(session, yyyymm=yyyymm)


@router.get("/vat-summary")
async def vat_summary(start: date, end: date,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """부가세 신고 집계 보조 (P2-⑦) — 전자세금계산서 매출·매입 세액 raw(안분 전)."""
    _require(user)
    return await _svc.vat_summary(session, start=start, end=end)


@router.get("/insurance-filing")
async def insurance_filing(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    _require(user)
    return await _svc.insurance_filing(session, yyyymm=yyyymm)


@router.get("/report.xlsx")
async def tax_report_xlsx(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """세무 신고서식 Excel — 원천징수이행상황 + 4대보험 집계 (P1-1)."""
    from fastapi.responses import StreamingResponse

    from erp.common.excel import build_xlsx

    _require(user)
    wh = await _svc.withholding(session, yyyymm=yyyymm)
    fl = await _svc.insurance_filing(session, yyyymm=yyyymm)
    # 신고서식 기관정보 헤더 — 기관 설정(/admin/org) 연계. 미설정 항목은 행 생략(견본값 미출력).
    # 기관명은 org_display_name(DB > env ORG_NAME > None) — 증명서·급여명세·계약서와 동일 계층.
    # (get()만 쓰면 DB 미설정·env만 있는 상태에서 기관명이 이 산출물에서만 무음 누락되던 불일치 방지)
    from erp.common.org_service import OrgSettingsService, org_display_name
    name = await org_display_name(session)
    org = await OrgSettingsService().get(session)   # 사업자번호·대표자는 DB 설정에만 존재
    rows = []
    if name:
        rows.append(["기관", "상호(법인명)", name, ""])
    if org is not None:
        if org.biz_reg_no:
            rows.append(["기관", "사업자등록번호", org.biz_reg_no, ""])
        if org.representative:
            rows.append(["기관", "대표자", org.representative, ""])
    rows += [
        ["원천징수이행상황", "소득구분", wh["소득구분"], ""],
        ["원천징수이행상황", "인원(명)", wh["인원"], ""],
        ["원천징수이행상황", "총지급액(원)", wh["총지급액"], ""],
        ["원천징수이행상황", "원천징수세액(원)", wh["원천징수세액"], ""],
    ]
    payable = fl.get("공단납부_추정_노사합산", {})
    for k in ["국민연금", "건강보험", "장기요양", "고용보험"]:
        p = payable.get(k)
        rows.append(["4대보험", f"{k} 근로자부담", fl["근로자부담"].get(k, 0),
                     f"공단납부추정 {p:,.0f}" if p else "사업주요율 별도"])
    rows.append(["4대보험", "근로자부담 합계", fl["근로자부담"].get("합계", 0), f"대상 {fl['인원']}명"])
    content = build_xlsx(sheet_name=f"세무신고-{yyyymm}",
                         headers=["구분", "항목", "값", "비고"], rows=rows)
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="tax-report-{yyyymm}.xlsx"'})


@router.get("/business-status")
async def business_status(year: int, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """사업장현황신고 집계(A-2) — 수입(수익계정 월별)·매입(세금계산서)·인건비."""
    _require(user)
    return await _svc.business_status(session, year=year)


# 의료비 연말정산 EP는 patient 모듈로 이관(Phase0 결정 2026-07-23·tax location-free化):
# GET /patients/medical-expenses(.xlsx). 환자 chart_no+실명 PHI라 PHI-bound patient 소속.
