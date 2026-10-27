"""Finance 라우터 — 전표·예산·정산 (design §4.3)."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.kst import kst_iso
from erp.common.months import YearMonth
from erp.common.policy import ScopeKind, require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.errors import ValidationError
from erp.finance.models import Voucher
from erp.finance.asset_service import AssetService
from erp.finance.report_service import FinanceReportService
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.loan_service import LoanService
from erp.finance.settlement_service import SettlementService
from erp.finance.statements import StatementService

router = APIRouter(prefix="/finance", tags=["finance"])
_vouchers, _reports = VoucherService(), FinanceReportService()
from erp.audit.service import AuditService  # noqa: E402

_audit = AuditService()


async def _money_audit(session, user, action: str, entity: str, target_id, after=None):
    """재무 변경 감사추적(P0-8) — 전표·마감·결산·예산·자산 머니 변경 기록."""
    await _audit.record_audit(session, actor=user.user_id, action=action,
                              entity=entity, target_id=str(target_id), after=after)
_statements = StatementService()
_assets = AssetService()
_settle = SettlementService()
from erp.finance.allowance_service import AllowanceService  # noqa: E402
from erp.finance.deferred_tax_service import DeferredTaxService  # noqa: E402
from erp.finance.disclosure_service import DisclosureService  # noqa: E402
from erp.finance.revenue_recognition_service import RevenueRecognitionService  # noqa: E402
from erp.finance.forex_service import ForexTranslationService  # noqa: E402
from erp.finance.intangible_service import IntangibleAssetService  # noqa: E402

_allowance = AllowanceService()
_loans = LoanService()
_deferred_tax = DeferredTaxService()
_disclosure = DisclosureService()
_revrec = RevenueRecognitionService()
_forex = ForexTranslationService()
_intangible = IntangibleAssetService()


def _require(user: CurrentUser, action: str, resource: str = "finance.vouchers"):
    """단일 권한 게이트 위임 (M5 수렴) — 인자 순서는 도메인 관례(action, resource) 유지."""
    return require_scope(user, resource, action)


class VoucherLineBody(BaseModel):
    account_code: str
    side: str
    amount: float
    department_id: int | None = None
    detail: str | None = None
    cost_center: str | None = None   # 라인 확장 차원(G9)
    project: str | None = None


class VoucherBody(BaseModel):
    voucher_on: date
    lines: list[VoucherLineBody]
    memo: str = ""


class ReverseBody(BaseModel):
    reason: str


@router.get("/allowance/preview")
async def allowance_preview(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """대손충당금 미리보기(K-IFRS 1109) — 연령버킷×요율 목표충당·현재·조정차액."""
    _require(user, "read", "finance.periods")
    return await _allowance.preview(session, yyyymm=yyyymm)


@router.post("/allowance/overlay")
async def allowance_overlay(factor: float, user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """대손 ECL 거시전망 오버레이 계수 설정(K-IFRS 1109, forward-looking)."""
    _require(user, "write", "finance.periods")
    try:
        return await _allowance.set_overlay(session, factor=factor, actor=user.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/allowance/post")
async def allowance_post(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """대손충당금 조정 결산분개 전기(멱등) — 증가 5690/1190·환입 1190/5690."""
    _require(user, "write", "finance.periods")
    res = await _allowance.post(session, yyyymm=yyyymm, actor=user.user_id)
    if res.get("posted"):
        await _money_audit(session, user, "ALLOWANCE_POST", "finance.periods",
                           res.get("voucher_id"), after={"yyyymm": yyyymm, "delta": res["delta"]})
    return res


@router.get("/forex/preview")
async def forex_preview(account_id: int, fc_balance: float, rate: float, yyyymm: YearMonth,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """외화 기말환산 미리보기(K-IFRS 1021) — 외화잔액×기말환율 vs 장부가 환산차."""
    _require(user, "read", "finance.periods")
    try:
        return await _forex.preview(session, account_id=account_id, fc_balance=fc_balance,
                                    rate=rate, yyyymm=yyyymm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/forex/post")
async def forex_post(account_id: int, fc_balance: float, rate: float, yyyymm: YearMonth,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """외화 기말환산 전기(멱등) — 환산차를 외화환산이익(4650)/손실(5670)·GL."""
    _require(user, "write", "finance.periods")
    try:
        res = await _forex.post(session, account_id=account_id, fc_balance=fc_balance,
                                rate=rate, yyyymm=yyyymm, actor=user.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if res.get("posted"):
        # 실제 전기 결과를 감사에 남긴다(2026-10-09 전체 흐름 검토 D·MED-2) — 근거 입력(환율·외화잔액·
        # 장부가)까지 있어야 사후에 환산차가 어디서 왔는지 재현할 수 있다.
        await _money_audit(session, user, "FX_TRANSLATE", "finance.periods",
                           res.get("voucher_id"),
                           after={"yyyymm": yyyymm, "account_id": account_id,
                                  "delta": res["delta"], "rate": res.get("rate"),
                                  "fc_balance": res.get("fc_balance"),
                                  "book_value": res.get("book_value")})
    return res


class FxMovementBody(BaseModel):
    account_id: int
    direction: str        # IN(입금)|OUT(출금)
    fc_amount: float
    rate: float
    on: date


@router.post("/forex/movements", status_code=201)
async def forex_movement(body: FxMovementBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """외화 입출금 라우팅(Stage2) — 통화별 계좌 GL로 부킹(취득환율). 기말환산은 /forex/post."""
    _require(user, "write", "finance.periods")
    return await _forex.record_movement(
        session, account_id=body.account_id, direction=body.direction,
        fc_amount=body.fc_amount, rate=body.rate, on=body.on, actor=user.user_id)


@router.get("/unbilled-revenue/status")
async def unbilled_status(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """미청구수익 발생 현황(K-IFRS 1115)."""
    _require(user, "read", "finance.periods")
    return await _revrec.status(session, yyyymm=yyyymm)


@router.post("/unbilled-revenue/accrue")
async def unbilled_accrue(yyyymm: YearMonth, amount: float,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """기간말 미청구수익 발생 + 차기 환입(발생주의, K-IFRS 1115)."""
    _require(user, "write", "finance.periods")
    try:
        res = await _revrec.accrue_unbilled(session, yyyymm=yyyymm, amount=amount,
                                            actor=user.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await _money_audit(session, user, "UNBILLED_ACCRUE", "finance.periods",
                       res.get("accrual_voucher"), after={"yyyymm": yyyymm, "amount": res["amount"]})
    return res


@router.get("/deferred-tax/preview")
async def deferred_tax_preview(year: int, user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """이연법인세 미리보기(K-IFRS 1012) — 일시적차이·DTA/DTL·조정차액."""
    _require(user, "read", "finance.periods")
    return await _deferred_tax.preview(session, year=year)


@router.post("/deferred-tax/post")
async def deferred_tax_post(year: int, user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """이연법인세 조정 결산분개 전기(멱등) — 연말 측정."""
    _require(user, "write", "finance.periods")
    res = await _deferred_tax.post(session, year=year, actor=user.user_id)
    if res.get("posted"):
        await _money_audit(session, user, "DEFERRED_TAX_POST", "finance.periods",
                           res.get("voucher_id"),
                           after={"year": year, "dta": res["dta"], "dtl": res["dtl"]})
    return res


@router.get("/dimensions/summary")
async def dimension_summary(dim: str, since: date, until: date,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """라인 확장 차원(G9)별 집계 — cost_center|project 기준 차·대·순액(POSTED+REVERSED)."""
    _require(user, "read")
    if dim not in ("cost_center", "project"):
        raise ValidationError("dim은 cost_center|project 입니다")
    return await _statements.dimension_summary(session, dim=dim, since=since, until=until)


@router.post("/vouchers", status_code=201)
async def create_voucher(body: VoucherBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """수동 전표 초안 (M2 maker-checker) — DRAFT 생성(미전기). 상신·승인 후 전기.

    자동전표(patient/claims/scm 등)는 원천 통제로 즉시 POSTED — 본 경로(수동)만 승인 대상."""
    _require(user, "write")
    import uuid
    v = await _vouchers.create_draft(
        session, source_ref=f"manual-{uuid.uuid4().hex[:12]}",
        voucher_on=body.voucher_on, memo=body.memo, created_by=user.user_id,
        lines=[VoucherLineDTO(**ln.model_dump()) for ln in body.lines])
    await _money_audit(session, user, "VOUCHER_DRAFT", "finance.vouchers", v.id,
                       after={"memo": body.memo, "lines": len(body.lines), "status": "DRAFT"})
    return {"id": v.id, "status": v.status}


@router.post("/vouchers/{voucher_id}/submit")
async def submit_voucher(voucher_id: int,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """초안 상신 — DRAFT→PENDING."""
    _require(user, "write")
    v = await _vouchers.submit(session, voucher_id=voucher_id, actor=user.user_id)
    await _money_audit(session, user, "VOUCHER_SUBMIT", "finance.vouchers", voucher_id,
                       after={"status": v.status})
    return {"id": v.id, "status": v.status}


@router.post("/vouchers/{voucher_id}/approve")
async def approve_voucher(voucher_id: int, override: bool = False,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """승인=전기 — PENDING→POSTED. 작성자≠승인자(SoD)·마감가드·차대검증.

    opt-in 다단계: VOUCHER 결재룰 있으면 단계 승인(완주 시 전기)·override=DIRECTOR 전권."""
    # 2026-10-09 시나리오 S-E D1: 결재선 단계 적격자(MANAGER·DIRECTOR 등)는 전표 write 권한이 없어도 승인
    from erp.common.approval_engine import require_scope_or_step_approver
    await require_scope_or_step_approver(session, user, "finance.vouchers", "write",
                                         doc_type="VOUCHER", doc_id=voucher_id,
                                         override=override)
    v = await _vouchers.approve(session, voucher_id=voucher_id, actor=user.user_id,
                                actor_role=user.role, override=override)
    await _money_audit(session, user, "VOUCHER_APPROVE", "finance.vouchers", voucher_id,
                       after={"status": v.status})
    return {"id": v.id, "status": v.status}


@router.post("/vouchers/{voucher_id}/reject")
async def reject_voucher(voucher_id: int, body: ReverseBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """반려 — PENDING→REJECTED (사유 필수, ReverseBody.reason 재사용)."""
    from erp.common.approval_engine import require_scope_or_step_approver
    await require_scope_or_step_approver(session, user, "finance.vouchers", "write",
                                         doc_type="VOUCHER", doc_id=voucher_id)
    v = await _vouchers.reject(session, voucher_id=voucher_id, reason=body.reason,
                               actor=user.user_id)
    await _money_audit(session, user, "VOUCHER_REJECT", "finance.vouchers", voucher_id,
                       after={"status": v.status, "reason": body.reason})
    return {"id": v.id, "status": v.status}


@router.post("/vouchers/{voucher_id}/reverse", status_code=201)
async def reverse_voucher(voucher_id: int, body: ReverseBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """역분개 (FR-54) — 마감 후 정정 경로. 원 전표 보존 + 반대 분개. 사유 필수·감사."""
    _require(user, "write")
    if not (body.reason or "").strip():
        raise ValidationError("역분개 사유는 필수입니다")
    # 2026-10-09 시나리오 3회차: 청구 심사 결과 전표(HIS claim:review)를 손으로 역분개하면 GL만 바뀌고 청구
    # 보조원장(decided_amt)은 그대로라 둘이 어긋났다(finance는 claims를 고칠 수 없다). 심사 결과의 정정
    # 경로는 HIS 재심사 — 재수신하면 ERP가 정정 전표와 결정액을 함께 바꾼다.
    target = await session.get(Voucher, voucher_id)
    if target is not None and target.source_module == "his.claim" \
            and (target.source_ref or "").startswith("claim:review:"):
        raise ValidationError(
            "청구 심사 결과 전표는 직접 역분개할 수 없습니다 — 심사팀이 HIS에서 재심사(정정) 처리하면 "
            "ERP가 정정 전표와 결정액을 함께 반영합니다")
    rev = await _vouchers.reverse(session, voucher_id=voucher_id, reason=body.reason,
                                  created_by=user.user_id)
    await _money_audit(session, user, "REVERSE", "finance.vouchers", voucher_id,
                       after={"reversal_id": rev.id, "reason": body.reason})
    return {"id": rev.id, "reverses": voucher_id}


@router.get("/vouchers")
async def list_vouchers(user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session),
                        page: int = 1, size: int = 50):
    _require(user, "read")
    from sqlalchemy import func

    from erp.finance.models import VoucherLine
    size = min(size, 200)
    rows = (await session.execute(
        select(Voucher).order_by(Voucher.id.desc())
        .offset((page - 1) * size).limit(size))).scalars().all()
    # 전표별 총액(차변 합) — 승인자가 금액을 보고 결재하도록(내부통제·전체검토 X0-3).
    ids = [v.id for v in rows]
    totals: dict[int, float] = {}
    if ids:
        for vid, amt in (await session.execute(
            select(VoucherLine.voucher_id, func.coalesce(func.sum(VoucherLine.amount), 0))
            .where(VoucherLine.voucher_id.in_(ids), VoucherLine.side == "DEBIT")
            .group_by(VoucherLine.voucher_id))).all():
            totals[vid] = float(amt)

    def _amount(v: Voucher) -> float:
        """DRAFT/PENDING은 VoucherLine이 없다 — draft_lines에서 차변 합을 낸다.

        M2 maker-checker는 승인 시점에만 전기하므로(service.create_draft) 승인 **대상** 전표의
        금액이 정확히 0원으로 표시됐다 — 승인자가 금액을 모른 채 결재하던 경로.
        """
        posted = totals.get(v.id)
        if posted:
            return posted
        return sum(float(ln.get("amount", 0)) for ln in (v.draft_lines or [])
                   if ln.get("side") == "DEBIT")

    return {"items": [{"id": v.id, "voucher_on": v.voucher_on, "source": v.source_module,
                       "source_ref": v.source_ref, "status": v.status, "memo": v.memo,
                       "amount": _amount(v),
                       # 통제 증적 — 모델엔 있는데 응답에서 탈락해 SoD(작성자≠승인자) 준수를
                       # 화면에서 증명할 수 없었고, 역분개 연결·반려 사유도 되읽을 수 없었다.
                       "created_by": v.created_by, "submitted_by": v.submitted_by,
                       "approved_by": v.approved_by, "approved_at": kst_iso(v.approved_at),
                       "reject_reason": v.reject_reason,
                       "reverses": v.reverses, "reversed_by": v.reversed_by}
                      for v in rows], "page": page, "size": size}


async def _require_voucher_read(session: AsyncSession, user: CurrentUser, voucher_id: int):
    """전표 **단건** 열람 — finance.vouchers read 또는 그 전표 결재선의 현재 단계 적격 결재자.

    2026-10-09 시나리오(메뉴·권한 정합): 결재선 ROLE 단계(MANAGER·DIRECTOR)는 승인 EP는 통과
    (require_scope_or_step_approver)하는데 정작 승인 대상 전표의 금액·분개를 열 수 없어 내용을
    모른 채 결재해야 했다. 승인과 같은 판정으로 **그 전표 한 건**만 연다 — 원장 목록(/vouchers)은
    종전대로 finance.vouchers read 전용(결재자에게 원장 전체를 여는 것이 아님)."""
    from erp.common.approval_engine import require_scope_or_step_approver
    return await require_scope_or_step_approver(session, user, "finance.vouchers", "read",
                                                doc_type="VOUCHER", doc_id=voucher_id)


@router.get("/vouchers/{voucher_id}")
async def voucher_detail(voucher_id: int,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """전표 단건(머리) — 결재함에서 승인 대상 전표를 여는 용도. 분개는 /lines."""
    await _require_voucher_read(session, user, voucher_id)
    v = await session.get(Voucher, voucher_id)
    if v is None:
        raise HTTPException(status_code=404, detail="전표가 없습니다")
    from sqlalchemy import func

    from erp.finance.models import VoucherLine
    posted = float(await session.scalar(
        select(func.coalesce(func.sum(VoucherLine.amount), 0))
        .where(VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT")) or 0)
    amount = posted or sum(float(ln.get("amount", 0)) for ln in (v.draft_lines or [])
                           if ln.get("side") == "DEBIT")
    return {"id": v.id, "voucher_on": v.voucher_on, "source": v.source_module,
            "source_ref": v.source_ref, "status": v.status, "memo": v.memo, "amount": amount,
            "created_by": v.created_by, "submitted_by": v.submitted_by,
            "approved_by": v.approved_by, "approved_at": kst_iso(v.approved_at),
            "reject_reason": v.reject_reason}


@router.get("/vouchers/{voucher_id}/lines")
async def voucher_lines(voucher_id: int,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """전표 분개 라인 — 승인 전 금액·계정 확인용(내부통제·전체검토 X0-3).

    DRAFT/PENDING은 원장 라인이 없고 `draft_lines`(JSON)에만 분개안이 있다. VoucherLine만 조회하면
    **정작 승인 대상 전표에서 빈 표**가 떠 이 EP의 목적이 무산되므로 초안을 폴백으로 반환한다
    (`draft=true`로 미전기임을 명시). 작성 폼이 입력받는 차원(코스트센터·프로젝트·부서)도 함께.
    """
    await _require_voucher_read(session, user, voucher_id)
    from erp.finance.models import VoucherLine
    rows = (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == voucher_id)
        .order_by(VoucherLine.side.desc(), VoucherLine.id))).scalars().all()
    if rows:
        return {"draft": False,
                "items": [{"account_code": ln.account_code, "side": ln.side,
                           "amount": float(ln.amount), "detail": ln.detail,
                           "cost_center": ln.cost_center, "project": ln.project,
                           "department_id": ln.department_id} for ln in rows]}
    v = await session.get(Voucher, voucher_id)
    draft = (v.draft_lines or []) if v is not None else []
    return {"draft": bool(draft),
            "items": [{"account_code": ln.get("account_code"), "side": ln.get("side"),
                       "amount": float(ln.get("amount", 0)), "detail": ln.get("detail"),
                       "cost_center": ln.get("cost_center"), "project": ln.get("project"),
                       "department_id": ln.get("department_id")}
                      for ln in sorted(draft, key=lambda x: x.get("side") != "DEBIT")]}


class BudgetBody(BaseModel):
    department_id: int
    fiscal_year: int
    account_code: str
    amount: float
    reason: str = ""


def _budget_service():
    from erp.finance.budget_service import BudgetService
    return BudgetService()


@router.put("/budgets")
async def upsert_budget(body: BudgetBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """예산 증액/감액 — 변경 이력(BudgetAmendment) 자동 기록."""
    _require(user, "write", "finance.budgets")
    out = await _budget_service().amend(
        session, department_id=body.department_id, fiscal_year=body.fiscal_year,
        account_code=body.account_code, new_amount=body.amount, reason=body.reason,
        user_id=user.user_id)
    await _money_audit(session, user, "BUDGET_AMEND", "finance.budgets",
                       f"{body.fiscal_year}:{body.department_id}:{body.account_code}",
                       after={"new_amount": body.amount, "reason": body.reason})
    await session.commit()
    return out


class BudgetTransferBody(BaseModel):
    department_id: int
    fiscal_year: int
    from_account: str
    to_account: str
    amount: float
    reason: str = ""


@router.post("/budgets/transfer")
async def transfer_budget(body: BudgetTransferBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """예산 전용 — 과목 간 이전(감액·증액 짝, 원 과목 가용 검증)."""
    _require(user, "write", "finance.budgets")
    out = await _budget_service().transfer(
        session, department_id=body.department_id, fiscal_year=body.fiscal_year,
        from_account=body.from_account, to_account=body.to_account, amount=body.amount,
        reason=body.reason, user_id=user.user_id)
    await _money_audit(session, user, "BUDGET_TRANSFER", "finance.budgets",
                       f"{body.fiscal_year}:{body.department_id}", after=out)
    await session.commit()
    return out


@router.post("/budgets/carry-over")
async def carry_over_budget(department_id: int, from_year: int,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """예산 이월 — 전년 미집행 잔액을 차년도로(멱등)."""
    _require(user, "write", "finance.budgets")
    out = await _budget_service().carry_over(
        session, department_id=department_id, from_year=from_year, user_id=user.user_id)
    if out["carried"]:
        await _money_audit(session, user, "BUDGET_CARRYOVER", "finance.budgets",
                           f"{from_year}:{department_id}", after={"total": out["total"]})
    await session.commit()
    return out


class EncumbranceBody(BaseModel):
    department_id: int
    fiscal_year: int
    account_code: str
    amount: float
    description: str = ""
    source_ref: str | None = None


@router.post("/budgets/encumbrances")
async def create_encumbrance(body: EncumbranceBody,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """예산 약정 등록 — 지출예정 예약(가용예산 차감)."""
    _require(user, "write", "finance.budgets")
    enc = await _budget_service().encumber(
        session, department_id=body.department_id, fiscal_year=body.fiscal_year,
        account_code=body.account_code, amount=body.amount, description=body.description,
        source_ref=body.source_ref, user_id=user.user_id)
    await session.commit()
    return {"id": enc.id, "status": enc.status}


@router.get("/budgets/encumbrances")
async def list_encumbrances(fiscal_year: int, department_id: int | None = None,
                            status: str = "OPEN",
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """약정 목록 — 해제 대상 선택용. 부서장은 본부서만(예산 read와 동일 scope)."""
    scope = require_scope(user, "finance.budgets", "read")
    dept = scope.dept_id if scope.kind == ScopeKind.DEPT else department_id
    return {"items": await _budget_service().list_encumbrances(
        session, fiscal_year=fiscal_year, department_id=dept, status=status)}


@router.post("/budgets/encumbrances/{enc_id}/release")
async def release_encumbrance(enc_id: int,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """약정 해제 — 집행 완료/취소 시 가용예산 복원."""
    _require(user, "write", "finance.budgets")
    enc = await _budget_service().release(session, encumbrance_id=enc_id)
    await session.commit()
    return {"id": enc.id, "status": enc.status}


@router.get("/budgets/amendments")
async def budget_amendments(fiscal_year: int, department_id: int | None = None,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """예산 변경 이력 — 부서장은 본부서만(집행률과 동일 scope, design §6.2)."""
    scope = require_scope(user, "finance.budgets", "read")
    # DEPT 스코프는 본부서 강제(사용자 입력 department_id 우회 차단), ALL은 입력 필터 허용
    dept = scope.dept_id if scope.kind == ScopeKind.DEPT else department_id
    return {"items": await _budget_service().amendments(
        session, fiscal_year=fiscal_year, department_id=dept)}


@router.get("/budgets/execution")
async def budget_execution(fiscal_year: int,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """집행률 (FR-09) — 부서장은 본부서만 (집계 명시 WHERE, design §6.2)."""
    # 분석 M4 — 정책 차용(hr.duties) 제거: 전용 finance.budgets read 정책 사용
    scope = require_scope(user, "finance.budgets", "read")
    rows = await _reports.budget_execution(
        session, fiscal_year=fiscal_year,
        scope_dept_id=scope.dept_id if scope.kind == ScopeKind.DEPT else None)
    return {"fiscal_year": fiscal_year, "items": rows}


@router.get("/reports/monthly/{yyyymm}")
async def monthly_report(yyyymm: YearMonth,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    return await _reports.monthly_summary(session, yyyymm=yyyymm)


# ── 재무제표·결산 (FR-08 확장) ─────────────────────────────
@router.get("/statements/trial-balance")
async def trial_balance(start: date, end: date,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """시산표 — 기간 계정별 차변·대변(복식부기 검증)."""
    _require(user, "read", "finance.statements")
    return await _statements.trial_balance(session, since=start, until=end)


@router.get("/statements/trial-balance.xlsx")
async def trial_balance_xlsx(start: date, end: date,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """시산표 Excel 출력 (P2 X1 — 출력 일관)."""
    from fastapi.responses import StreamingResponse

    from erp.common.excel import build_xlsx
    _require(user, "read", "finance.statements")
    tb = await _statements.trial_balance(session, since=start, until=end)
    rows = [[a["account_code"], a["name"], a["kind"], a["debit"], a["credit"]]
            for a in tb["accounts"]]
    rows.append(["", "합계", "", tb["total_debit"], tb["total_credit"]])
    content = build_xlsx(sheet_name=f"시산표_{start}_{end}",
                         headers=["계정코드", "계정명", "구분", "차변(원)", "대변(원)"], rows=rows)
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="trial-balance-{start}.xlsx"'})


@router.get("/statements/income")
async def income_statement(start: date, end: date,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """손익계산서 — 기간 수익·비용·당기순이익."""
    _require(user, "read", "finance.statements")
    return await _statements.income_statement(session, since=start, until=end)


@router.get("/statements/operating")
async def operating_statement(start: date, end: date,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """의료기관 회계기준 운영성과표 — 의료이익·의료외손익·법인세차감전순이익·당기순이익."""
    _require(user, "read", "finance.statements")
    return await _statements.operating_statement(session, since=start, until=end)


@router.get("/statements/comprehensive-income")
async def comprehensive_income(start: date, end: date,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """포괄손익계산서(K-IFRS 1001) — 당기순이익 + 기타포괄손익(OCI) = 총포괄손익."""
    _require(user, "read", "finance.statements")
    return await _statements.comprehensive_income(session, since=start, until=end)


@router.get("/statements/comparative-balance-sheet")
async def comparative_balance_sheet(as_of: date,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """전기 비교 재무상태표(K-IFRS 1001) — 당기말·전기말 2기."""
    _require(user, "read", "finance.statements")
    return await _statements.comparative_balance_sheet(session, as_of=as_of)


@router.get("/statements/interim")
async def interim_report(year: int, quarter: int,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """중간재무보고(K-IFRS 1034) — 분기 손익·누적(YTD)·전년 동분기."""
    _require(user, "read", "finance.statements")
    return await _statements.interim_report(session, year=year, quarter=quarter)


@router.get("/disclosures")
async def disclosures(year: int, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """재무제표 주석(K-IFRS 1001) — 자동 정량주석 + 저장 정성주석."""
    _require(user, "read", "finance.statements")
    return await _disclosure.notes(session, year=year)


class DisclosureBody(BaseModel):
    note_key: str
    title: str
    payload: dict


@router.put("/disclosures")
async def save_disclosure(year: int, body: DisclosureBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """정성 주석 저장(회계정책 등) — (연도, note_key) upsert."""
    _require(user, "write", "finance.statements")
    return await _disclosure.save(session, year=year, note_key=body.note_key,
                                  title=body.title, payload=body.payload, actor=user.user_id)


class RelatedPartyBody(BaseModel):
    name: str
    relation: str
    note: str | None = None


class RPTxnBody(BaseModel):
    party_id: int
    period: str
    txn_type: str
    amount: float
    balance: float = 0
    note: str | None = None


class ContingencyBody(BaseModel):
    period: str
    kind: str = "LIABILITY"
    description: str
    estimate: float | None = None
    likelihood: str = "중간"


class SubsequentEventBody(BaseModel):
    period: str
    event_date: date
    description: str
    adjusting: bool = False
    reflected: bool = False


@router.post("/related-parties")
async def add_related_party(body: RelatedPartyBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """특수관계자 등록(K-IFRS 1024)."""
    _require(user, "write", "finance.statements")
    return await _disclosure.add_related_party(session, **body.model_dump())


@router.post("/related-parties/transactions")
async def add_rp_txn(body: RPTxnBody, user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """특수관계자 거래·잔액 등록(K-IFRS 1024)."""
    _require(user, "write", "finance.statements")
    return await _disclosure.add_rp_txn(session, **body.model_dump())


@router.get("/related-parties/suggestions")
async def related_party_suggestions(year: int, user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """특수관계자 거래 자동 탐지(보조, K-IFRS 1024) — 전표 적요 이름매칭(휴먼 확인 필요)."""
    _require(user, "read", "finance.statements")
    return await _disclosure.suggest_related_party_txns(session, year=year)


@router.get("/ifrs-activity")
async def ifrs_activity(year: int, user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """당해 IFRS 측정·조정 활동(기준별 금액·건수) — 플로팅 IFRS 마크 동적 표시용."""
    _require(user, "read", "finance.statements")
    return await _disclosure.ifrs_activity(session, year=year)


@router.get("/ifrs-conformance")
async def ifrs_conformance(year: int, user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """IFRS 정합 현황 — 기준 카탈로그(처리·적용화면·상태) + 당해 활동. 'IFRS 정합 현황' 화면용."""
    _require(user, "read", "finance.statements")
    return await _disclosure.ifrs_conformance(session, year=year)


@router.post("/contingencies")
async def add_contingency(body: ContingencyBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """우발부채/우발자산 등록(K-IFRS 1037)."""
    _require(user, "write", "finance.statements")
    return await _disclosure.add_contingency(session, **body.model_dump())


@router.post("/contingencies/{contingency_id}/recognize-provision")
async def recognize_provision(contingency_id: int, on_date: date,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """우발부채 → 충당부채 전환(K-IFRS 1037) — 확률 높음·추정가능 시 5680/2130 전기."""
    _require(user, "write", "finance.statements")
    out = await _disclosure.recognize_provision(session, contingency_id=contingency_id,
                                                on_date=on_date, user_id=user.user_id)
    await _money_audit(session, user, "CONTINGENCY_PROVISION", "finance.contingencies",
                       contingency_id, after={"on_date": str(on_date)})
    return out


@router.post("/subsequent-events")
async def add_subsequent_event(body: SubsequentEventBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """보고기간후사건 등록(K-IFRS 1010)."""
    _require(user, "write", "finance.statements")
    return await _disclosure.add_subsequent_event(session, **body.model_dump())


class AccountingChangeBody(BaseModel):
    period: str
    change_type: str            # POLICY|ESTIMATE|ERROR
    description: str
    treatment: str = "PROSPECTIVE"   # RETROSPECTIVE|PROSPECTIVE
    standard: str | None = None
    effect_amount: float | None = None


@router.post("/accounting-changes")
async def add_accounting_change(body: AccountingChangeBody,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """회계정책·추정변경·오류 등록(K-IFRS 1008)."""
    _require(user, "write", "finance.statements")
    return await _disclosure.add_accounting_change(session, **body.model_dump())


@router.get("/statements/balance-sheet")
async def balance_sheet(as_of: date,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """재무상태표 — 기준일 누적 자산·부채·자본."""
    _require(user, "read", "finance.statements")
    return await _statements.balance_sheet(session, as_of=as_of)


@router.get("/statements/financial-ratios")
async def financial_ratios(year: int,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """재무비율·경영 KPI (P1-⑤) — 유동/부채/자기자본비율·의료이익률·인건비율·재료비율(라이브 원본).

    대시보드/분석 화면은 BI 마트 서빙 `/analytics/financial-ratios`를 사용(성능검토 H-3). 이 EP는
    마감/공시용 라이브 원본."""
    _require(user, "read", "finance.statements")
    return await _statements.financial_ratios(session, year=year)


@router.get("/statements/cash-flow-statement")
async def cash_flow_statement(start: date, end: date,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """현금흐름표(간접법) — 영업/투자/재무 + 기초·기말현금 정합검증(reconciled).

    ※ /cash-flow(자금수지·자금일보)와 별개 — 본 EP가 정식 재무제표(4대 완성)."""
    _require(user, "read", "finance.statements")
    return await _statements.cash_flow_statement(session, since=start, until=end)


@router.get("/statements/cash-flow-direct")
async def cash_flow_direct(start: date, end: date,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """현금흐름표(직접법, K-IFRS 1007) — 영업 수취·지급 직접 분류 + 정합검증."""
    _require(user, "read", "finance.statements")
    return await _statements.cash_flow_direct(session, since=start, until=end)


@router.get("/statements/cash-flow-statement.xlsx")
async def cash_flow_statement_xlsx(start: date, end: date,
                                   user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """현금흐름표 Excel 출력."""
    from fastapi.responses import StreamingResponse

    from erp.common.excel import build_xlsx
    _require(user, "read", "finance.statements")
    cf = await _statements.cash_flow_statement(session, since=start, until=end)
    rows = [["영업활동", "당기순이익", cf["operating"]["net_income"]]]
    rows += [["영업활동", i["name"], i["amount"]] for i in cf["operating"]["adjustments"]]
    rows.append(["영업활동", "소계", cf["operating"]["subtotal"]])
    rows += [["투자활동", i["name"], i["amount"]] for i in cf["investing"]["items"]]
    rows.append(["투자활동", "소계", cf["investing"]["subtotal"]])
    rows += [["재무활동", i["name"], i["amount"]] for i in cf["financing"]["items"]]
    rows.append(["재무활동", "소계", cf["financing"]["subtotal"]])
    rows += [["", "현금 순증감", cf["net_change"]], ["", "기초현금", cf["opening_cash"]],
             ["", "기말현금", cf["closing_cash"]]]
    content = build_xlsx(sheet_name=f"현금흐름표_{start}_{end}",
                         headers=["활동", "항목", "금액"], rows=rows)
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="cash-flow-{start}.xlsx"'})


# ── 고정자산·감가상각 (FR-09 확장) ─────────────────────────
class AssetBody(BaseModel):
    asset_no: str
    name: str
    category: str = "MEDICAL"
    acquired_on: date
    acquisition_cost: float
    useful_life_years: int
    salvage_value: float = 0
    method: str = "STRAIGHT"
    department_id: int | None = None
    payment_method: str = "PAYABLE"   # CASH | PAYABLE


@router.get("/assets")
async def list_assets(user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "read", "finance.assets")
    return {"items": await _assets.list_assets(session)}


@router.post("/assets", status_code=201)
async def register_asset(body: AssetBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """고정자산 등록 — 취득 전표 자동생성(의료장비/미지급)."""
    _require(user, "write", "finance.assets")
    a = await _assets.register(session, asset_no=body.asset_no, name=body.name,
                               category=body.category, acquired_on=body.acquired_on,
                               acquisition_cost=body.acquisition_cost,
                               useful_life_years=body.useful_life_years,
                               salvage_value=body.salvage_value, method=body.method,
                               department_id=body.department_id,
                               payment_method=body.payment_method, created_by=user.user_id,
                               # 2026-10-09 전체 흐름 검토 2차 E: 수기 취득만 AP 보조원장(Payable) 동반
                               create_payable=True)
    return {"id": a.id, "asset_no": a.asset_no}


@router.post("/assets/depreciation/{period}")
async def run_depreciation(period: str,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """월 감가상각 실행 — 감가상각비/누계액 전표 자동생성(멱등)."""
    _require(user, "write", "finance.assets")
    count = await _assets.run_depreciation(session, period=period, created_by=user.user_id)
    return {"period": period, "depreciated": count}


@router.post("/assets/{asset_id}/dispose")
async def dispose_asset(asset_id: int, disposed_on: date, disposal_amount: float = 0,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """처분 — 자산제거·감가누계 상계·처분손익 전표 생성."""
    _require(user, "write", "finance.assets")
    out = await _assets.dispose(session, asset_id=asset_id, disposed_on=disposed_on,
                                disposal_amount=disposal_amount, created_by=user.user_id)
    await _money_audit(session, user, "DISPOSE", "finance.fixed_assets", asset_id,
                       after={"disposed_on": str(disposed_on), "amount": disposal_amount})
    return out


@router.post("/assets/{asset_id}/impair")
async def impair_asset(asset_id: int, recoverable_amount: float, on_date: date,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """손상차손 — 회수가능액 < 장부가액 시 차액을 손상차손/손상누계 전기."""
    _require(user, "write", "finance.assets")
    out = await _assets.impair(session, asset_id=asset_id, recoverable_amount=recoverable_amount,
                               on_date=on_date, created_by=user.user_id)
    await _money_audit(session, user, "IMPAIR", "finance.fixed_assets", asset_id,
                       after={"recoverable": recoverable_amount, "on": str(on_date)})
    await session.commit()
    return out


class IntangibleBody(BaseModel):
    asset_no: str
    name: str
    acquired_on: date
    cost: float
    useful_life_years: int
    on_credit: bool = False


@router.get("/intangibles")
async def list_intangibles(user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """무형자산 현황(K-IFRS 1038)."""
    _require(user, "read", "finance.assets")
    return {"items": await _intangible.list_assets(session)}


@router.post("/intangibles", status_code=201)
async def create_intangible(body: IntangibleBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """무형자산 취득(K-IFRS 1038)."""
    _require(user, "write", "finance.assets")
    out = await _intangible.create(session, user_id=user.user_id, **body.model_dump())
    await _money_audit(session, user, "INTANGIBLE_ACQ", "finance.intangible_assets", out["id"])
    await session.commit()
    return out


@router.post("/intangibles/{asset_id}/amortize/{yyyymm}")
async def amortize_intangible(asset_id: int, yyyymm: YearMonth,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """무형자산 월 정액 상각(5215/1855)."""
    _require(user, "write", "finance.assets")
    out = await _intangible.amortize_monthly(session, asset_id=asset_id, period=yyyymm,
                                             user_id=user.user_id)
    await _money_audit(session, user, "INTANGIBLE_AMORTIZE", "finance.intangible_assets",
                       asset_id, after={"period": yyyymm})
    await session.commit()
    return out


@router.post("/intangibles/{asset_id}/capitalize-borrowing")
async def capitalize_borrowing(asset_id: int, amount: float, on_date: date,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """차입원가 자본화(K-IFRS 1023) — 적격자산 원가 가산."""
    _require(user, "write", "finance.assets")
    out = await _intangible.capitalize_borrowing(session, asset_id=asset_id, amount=amount,
                                                 on_date=on_date, user_id=user.user_id)
    await _money_audit(session, user, "BORROWING_CAPITALIZE", "finance.intangible_assets",
                       asset_id, after={"amount": amount})
    await session.commit()
    return out


@router.post("/intangibles/{asset_id}/impair")
async def impair_intangible(asset_id: int, recoverable_amount: float, on_date: date,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """무형자산 손상차손(K-IFRS 1036) — 회수가능액 < 장부가."""
    _require(user, "write", "finance.assets")
    out = await _intangible.impair(session, asset_id=asset_id,
                                   recoverable_amount=recoverable_amount, on_date=on_date,
                                   user_id=user.user_id)
    await _money_audit(session, user, "INTANGIBLE_IMPAIR", "finance.intangible_assets", asset_id,
                       after={"recoverable_amount": recoverable_amount})
    await session.commit()
    return out


@router.post("/intangibles/{asset_id}/reverse-impairment")
async def reverse_impairment_intangible(asset_id: int, recoverable_amount: float, on_date: date,
                                        user: CurrentUser = Depends(get_current_user),
                                        session: AsyncSession = Depends(get_session)):
    """무형자산 손상환입(K-IFRS 1036) — 손상누계 한도."""
    _require(user, "write", "finance.assets")
    out = await _intangible.reverse_impairment(session, asset_id=asset_id,
                                               recoverable_amount=recoverable_amount,
                                               on_date=on_date, user_id=user.user_id)
    await _money_audit(session, user, "INTANGIBLE_IMPAIR_REVERSAL", "finance.intangible_assets",
                       asset_id, after={"recoverable_amount": recoverable_amount})
    await session.commit()
    return out


@router.post("/intangibles/{asset_id}/dispose")
async def dispose_intangible(asset_id: int, disposed_on: date, disposal_amount: float = 0,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """무형자산 처분(K-IFRS 1038) — 제거·누계 상계·처분손익."""
    _require(user, "write", "finance.assets")
    out = await _intangible.dispose(session, asset_id=asset_id, disposed_on=disposed_on,
                                    disposal_amount=disposal_amount, user_id=user.user_id)
    await _money_audit(session, user, "INTANGIBLE_DISPOSE", "finance.intangible_assets", asset_id,
                       after={"disposal_amount": disposal_amount})
    await session.commit()
    return out


@router.post("/assets/{asset_id}/reverse-impairment")
async def reverse_impairment_asset(asset_id: int, recoverable_amount: float, on_date: date,
                                   user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """손상차손 환입(K-IFRS 1036) — 회수가능액 회복 시 손상누계 한도 내 환입(당기손익)."""
    _require(user, "write", "finance.assets")
    out = await _assets.reverse_impairment(session, asset_id=asset_id,
                                           recoverable_amount=recoverable_amount,
                                           on_date=on_date, created_by=user.user_id)
    await _money_audit(session, user, "IMPAIR_REVERSE", "finance.fixed_assets", asset_id,
                       after={"recoverable": recoverable_amount, "on": str(on_date)})
    await session.commit()
    return out


@router.post("/assets/{asset_id}/revalue")
async def revalue_asset(asset_id: int, fair_value: float, on_date: date,
                        fair_value_level: str = "3",
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """재평가 — 공정가치 대비 장부가액 차이를 재평가잉여금(증)·재평가손실(감)으로 전기. fair_value_level=공정가치 서열."""
    _require(user, "write", "finance.assets")
    out = await _assets.revalue(session, asset_id=asset_id, fair_value=fair_value,
                                on_date=on_date, fair_value_level=fair_value_level,
                                created_by=user.user_id)
    await _money_audit(session, user, "REVALUE", "finance.fixed_assets", asset_id,
                       after={"fair_value": fair_value, "on": str(on_date)})
    await session.commit()
    return out


# ── 채권/채무·자금관리 (FR-09 확장) ────────────────────────
class ReceivableBody(BaseModel):
    party: str
    amount: float
    billed_on: date
    due_on: date
    description: str | None = None
    revenue_account: str = "4110"


class PayableBody(BaseModel):
    party: str
    amount: float
    incurred_on: date
    due_on: date
    description: str | None = None
    expense_account: str = "5110"


@router.get("/receivables")
async def list_receivables(page: int = 1, size: int = 50,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    _require(user, "read", "finance.settlement")
    size = min(max(size, 1), 200)
    out = await _settle.list_open(session, kind="AR", page=page, size=size)
    return {"items": out["items"], "count": out["total"],
            "page": out["page"], "size": out["size"]}


@router.post("/receivables", status_code=201)
async def add_receivable(body: ReceivableBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _require(user, "write", "finance.settlement")
    r = await _settle.record_receivable(
        session, party=body.party, amount=body.amount, billed_on=body.billed_on,
        due_on=body.due_on, description=body.description,
        revenue_account=body.revenue_account, created_by=user.user_id)
    return {"id": r.id, "status": r.status}


@router.post("/receivables/{receivable_id}/collect")
async def collect_receivable(receivable_id: int, amount: float, on: date,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    _require(user, "write", "finance.settlement")
    r = await _settle.collect(session, receivable_id=receivable_id, amount=amount, on=on,
                              created_by=user.user_id)
    return {"id": r.id, "collected": float(r.collected_amount), "status": r.status}


@router.get("/payables")
async def list_payables(page: int = 1, size: int = 50,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "read", "finance.settlement")
    size = min(max(size, 1), 200)
    out = await _settle.list_open(session, kind="AP", page=page, size=size)
    return {"items": out["items"], "count": out["total"],
            "page": out["page"], "size": out["size"]}


@router.post("/payables", status_code=201)
async def add_payable(body: PayableBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "write", "finance.settlement")
    p = await _settle.record_payable(
        session, party=body.party, amount=body.amount, incurred_on=body.incurred_on,
        due_on=body.due_on, description=body.description,
        expense_account=body.expense_account, created_by=user.user_id)
    return {"id": p.id, "status": p.status}


@router.post("/payables/{payable_id}/pay")
async def pay_payable(payable_id: int, amount: float, on: date,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "write", "finance.settlement")
    # 2026-10-09 전체 흐름 검토 E: 지급결의(배치) 중인 미지급금의 개별 지급 = 배치 SoD 우회 → 차단
    await _disb().assert_not_in_open_batch(session, payable_id=payable_id)
    p = await _settle.pay(session, payable_id=payable_id, amount=amount, on=on,
                          created_by=user.user_id)
    return {"id": p.id, "paid": float(p.paid_amount), "status": p.status}


# ── AP 지급결의 배치 (펌뱅킹 이체·P3-4) ──────────────────────────
def _disb():
    from erp.finance.disbursement_service import DisbursementService
    return DisbursementService()


class DisbursementBody(BaseModel):
    payable_ids: list[int]
    scheduled_on: date
    note: str | None = None
    accounts: dict[int, str] | None = None   # {payable_id: 이체계좌}(선택)


@router.post("/disbursements", status_code=201)
async def create_disbursement(body: DisbursementBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """지급결의 생성 — 미지급금 다건 배치(DRAFT). 활성 배치 중복 포함 차단."""
    _require(user, "write", "finance.settlement")
    b = await _disb().create_batch(session, payable_ids=body.payable_ids,
                                   scheduled_on=body.scheduled_on, note=body.note,
                                   accounts=body.accounts, actor=user.user_id)
    await _money_audit(session, user, "DISB_CREATE", "finance.disbursements", b.id,
                       after={"batch_no": b.batch_no, "total": float(b.total_amount),
                              "lines": b.line_count})
    await session.commit()
    return {"id": b.id, "batch_no": b.batch_no, "status": b.status,
            "total_amount": float(b.total_amount), "line_count": b.line_count}


@router.get("/disbursements")
async def list_disbursements(status: str | None = None,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    _require(user, "read", "finance.settlement")
    return {"items": await _disb().list_batches(session, status=status)}


@router.get("/disbursements/{batch_id}")
async def disbursement_detail(batch_id: int,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    _require(user, "read", "finance.settlement")
    return await _disb().detail(session, batch_id=batch_id)


@router.post("/disbursements/{batch_id}/approve")
async def approve_disbursement(batch_id: int,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """지급결의 승인 — 직무분리(작성자≠승인자·서버 강제)."""
    _require(user, "write", "finance.settlement")
    b = await _disb().approve(session, batch_id=batch_id, actor=user.user_id)
    await _money_audit(session, user, "DISB_APPROVE", "finance.disbursements", b.id,
                       after={"status": b.status})
    await session.commit()
    return {"id": b.id, "status": b.status}


@router.post("/disbursements/{batch_id}/execute")
async def execute_disbursement(batch_id: int, on: date,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """지급 실행 — 라인별 지급 전기(DR 미지급/CR 현금). 멱등."""
    _require(user, "write", "finance.settlement")
    out = await _disb().execute(session, batch_id=batch_id, on=on, actor=user.user_id)
    await _money_audit(session, user, "DISB_EXECUTE", "finance.disbursements", batch_id,
                       after=out)
    await session.commit()
    return out


@router.post("/disbursements/{batch_id}/cancel")
async def cancel_disbursement(batch_id: int,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    _require(user, "write", "finance.settlement")
    b = await _disb().cancel(session, batch_id=batch_id)
    await session.commit()
    return {"id": b.id, "status": b.status}


@router.get("/disbursements/{batch_id}/file")
async def disbursement_file(batch_id: int,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """이체파일(표준 CSV) 다운로드 — 거래처·금액·예정일·계좌·미지급ID."""
    from fastapi.responses import Response
    _require(user, "read", "finance.settlement")
    csv = await _disb().export_csv(session, batch_id=batch_id)
    await _audit.record_access(session, actor=user.user_id, action="EXPORT",
                               entity="finance.disbursements", route=f"/disbursements/{batch_id}/file")
    return Response(content=csv.encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="disbursement-{batch_id}.csv"'})


@router.get("/statements/equity-changes")
async def equity_changes(year: int,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """기본금변동계산서 (의료기관 회계기준 규칙, A-3) — 자본 변동 + 정합 검증."""
    _require(user, "read", "finance.statements")
    return await _statements.equity_changes(session, fiscal_year=year)


@router.get("/statements/equity-changes.xlsx")
async def equity_changes_xlsx(year: int,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    from fastapi.responses import StreamingResponse

    from erp.common.excel import build_xlsx
    _require(user, "read", "finance.statements")
    out = await _statements.equity_changes(session, fiscal_year=year)
    rows = [[i["account_code"], i["name"], i["opening"], i["increase"],
             i["decrease"], i["closing"]] for i in out["items"]]
    rows.append(["", "합계", out["total_opening"], "", "", out["total_closing"]])
    buf = build_xlsx(sheet_name=f"{year} 기본금변동계산서",
                     headers=["계정", "과목", "기초잔액(원)", "증가(원)", "감소(원)", "기말잔액(원)"],
                     rows=rows)
    return StreamingResponse(
        iter([buf]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="equity-changes-{year}.xlsx"'})


# ── 전자세금계산서 수취·대사 (A-1) ───────────────────────
class TaxInvoiceBody(BaseModel):
    approval_no: str
    issue_date: date
    supplier_bizno: str
    supplier_name: str | None = None
    supply_amount: float
    vat_amount: float = 0
    invoice_type: str = "TAX"          # TAX(과세) | EXEMPT(면세 계산서) | ZERO(영세율)
    note: str | None = None


@router.post("/tax-invoices", status_code=201)
async def register_tax_invoice(body: TaxInvoiceBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """수취 세금계산서/계산서 등록 — 승인번호 멱등."""
    from erp.finance.tax_invoice_service import TaxInvoiceService
    _require(user, "write", "finance.taxinvoice")
    inv = await TaxInvoiceService().register(session, actor=user.user_id,
                                             **body.model_dump())
    return {"id": inv.id, "status": inv.status}


@router.post("/tax-invoices/upload.csv", status_code=201)
async def upload_tax_invoices(file: UploadFile,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """홈택스 매입 목록 CSV 일괄 등록 — 행별 격리·승인번호 멱등."""
    from erp.finance.tax_invoice_service import TaxInvoiceService
    _require(user, "write", "finance.taxinvoice")
    content = await file.read()
    return await TaxInvoiceService().upload_csv(session, actor=user.user_id,
                                                content=content)


@router.get("/tax-invoices")
async def list_tax_invoices(status: str | None = None, month: str | None = None,
                            page: int = 1, size: int = 50,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    from erp.finance.tax_invoice_service import TaxInvoiceService
    _require(user, "read", "finance.taxinvoice")
    size = min(max(size, 1), 200)
    out = await TaxInvoiceService().list(session, status=status, month=month,
                                         page=page, size=size)
    return {"count": out["total"], "items": out["items"],
            "page": out["page"], "size": out["size"]}


@router.get("/tax-invoices/summary")
async def tax_invoice_summary(year: int,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """월별 과세/면세 매입 집계(A-2 신고 기초) + 미대사 현황."""
    from erp.finance.tax_invoice_service import TaxInvoiceService
    _require(user, "read", "finance.taxinvoice")
    return await TaxInvoiceService().summary(session, year=year)


@router.post("/tax-invoices/{invoice_id}/match")
async def match_tax_invoice(invoice_id: int, payable_id: int,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """미지급금 대사 — 합계금액=채무액(전액/잔액) 검증, 지급 전 증빙 통제."""
    from erp.finance.tax_invoice_service import TaxInvoiceService
    _require(user, "write", "finance.taxinvoice")
    inv = await TaxInvoiceService().match(session, actor=user.user_id,
                                          invoice_id=invoice_id, payable_id=payable_id)
    return {"id": inv.id, "status": inv.status, "payable_id": inv.payable_id}


@router.post("/tax-invoices/{invoice_id}/unmatch")
async def unmatch_tax_invoice(invoice_id: int,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    from erp.finance.tax_invoice_service import TaxInvoiceService
    _require(user, "write", "finance.taxinvoice")
    inv = await TaxInvoiceService().unmatch(session, actor=user.user_id,
                                            invoice_id=invoice_id)
    return {"id": inv.id, "status": inv.status}


@router.get("/aging")
async def aging(as_of: date, kind: str = "AR",
                user: CurrentUser = Depends(get_current_user),
                session: AsyncSession = Depends(get_session)):
    """연령분석 — kind=AR(채권)|AP(채무)."""
    _require(user, "read", "finance.settlement")
    return await _settle.aging(session, as_of=as_of, kind=kind)


@router.get("/aging-by-party")
async def aging_by_party(as_of: date, kind: str = "AP",
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """거래처별 연령분석 매트릭스 — (거래처 × 버킷)·총잔액·최근 지급/회수 예정일."""
    _require(user, "read", "finance.settlement")
    return await _settle.aging_by_party(session, as_of=as_of, kind=kind)


@router.get("/cash-flow")
async def cash_flow(start: date, end: date,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """자금수지 — 현금 유입·유출(출처별)."""
    _require(user, "read", "finance.settlement")
    return await _settle.cash_flow(session, since=start, until=end)


@router.get("/daily-cash")
async def daily_cash(on: date,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """자금일보 — 계정별 기초잔액→입금→출금→기말잔액(출처별 병기). 자금담당 일일 시재 대사."""
    _require(user, "read", "finance.settlement")
    return await _settle.daily_cash(session, on=on)


# ── 회계 기간 마감 (월 결산) ──────────────────────────────────────────
_periods = None


def _period_service():
    global _periods
    if _periods is None:
        from erp.finance.period_service import PeriodService
        _periods = PeriodService()
    return _periods


class PeriodActionBody(BaseModel):
    note: str = ""
    enforce: bool = False    # M3 — True면 마감 전 점검 경고 시 차단


@router.get("/periods")
async def list_periods(year: int | None = None,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """회계 기간(월) 마감 현황 — 손익 스냅샷 포함."""
    _require(user, "read", "finance.periods")
    rows = await _period_service().list_periods(session, year=year)
    return {"items": [{"yyyymm": p.yyyymm, "fiscal_year": p.fiscal_year, "status": p.status,
                       "total_revenue": float(p.total_revenue) if p.total_revenue is not None else None,
                       "total_expense": float(p.total_expense) if p.total_expense is not None else None,
                       "net_income": float(p.net_income) if p.net_income is not None else None,
                       "voucher_count": p.voucher_count,
                       "closed_at": kst_iso(p.closed_at),
                       "note": p.note} for p in rows]}


@router.get("/periods/{yyyymm}/preclose-check")
async def preclose_check(yyyymm: YearMonth,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """마감 전 점검 (M3) — 미결전표·차대·감가·원가배부·세금계산서 경고 목록."""
    _require(user, "read", "finance.periods")
    return await _period_service().preclose_check(session, yyyymm=yyyymm)


@router.post("/periods/{yyyymm}/close")
async def close_period(yyyymm: YearMonth, body: PeriodActionBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """기간 마감 — 사전점검(enforce 시 차단)·손익 확정·재무제표 스냅샷·전표 차단(정정은 역분개)."""
    _require(user, "write", "finance.periods")
    p = await _period_service().close(session, yyyymm=yyyymm, user_id=user.user_id,
                                      note=body.note, enforce=body.enforce)
    await _money_audit(session, user, "PERIOD_CLOSE", "finance.periods", yyyymm,
                       after={"net_income": float(p.net_income or 0)})
    await session.commit()
    return {"yyyymm": p.yyyymm, "status": p.status, "net_income": float(p.net_income or 0),
            "voucher_count": p.voucher_count}


@router.get("/ledger-integrity/verify")
async def ledger_integrity_verify(user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """원장 무결성 검증(G11) — voucher_lines 해시 체인 재계산 대조. ok=False면 변조 의심."""
    _require(user, "read", "finance.vouchers")
    from erp.finance.ledger_integrity_service import LedgerIntegrityService
    return await LedgerIntegrityService().verify(session)


@router.post("/ledger-integrity/checkpoint")
async def ledger_integrity_checkpoint(user: CurrentUser = Depends(get_current_user),
                                      session: AsyncSession = Depends(get_session)):
    """원장 무결성 체크포인트 수동 생성(배치와 별개·즉시 봉인)."""
    _require(user, "write", "finance.vouchers")
    from erp.finance.ledger_integrity_service import LedgerIntegrityService
    out = await LedgerIntegrityService().checkpoint(session, actor=user.user_id)
    await session.commit()
    return out


@router.get("/periods/{yyyymm}/reconcile")
async def reconcile_period(yyyymm: YearMonth,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """마감 무결성 대사 — 확정 스냅샷 vs 현재 재도출 재무제표 드리프트(G6·S6). 읽기 전용."""
    _require(user, "read", "finance.periods")
    return await _period_service().reconcile(session, yyyymm=yyyymm)


@router.post("/periods/{yyyymm}/reopen")
async def reopen_period(yyyymm: YearMonth, body: PeriodActionBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """마감 재개방 — 정정 필요 시(사유 필수·감사·마감자≠재개자 SoD). 마감(write)과 분리된 approve 권한."""
    _require(user, "approve", "finance.periods")   # 마감 권한키 분리 — reopen은 상위 approve 권한
    if not (body.note or "").strip():
        raise ValidationError("마감 재개방은 사유(note)가 필수입니다")
    p = await _period_service().reopen(session, yyyymm=yyyymm, user_id=user.user_id, note=body.note)
    await _money_audit(session, user, "PERIOD_REOPEN", "finance.periods", yyyymm,
                       after={"reason": body.note, "reopen_count": p.reopen_count})
    await session.commit()
    return {"yyyymm": p.yyyymm, "status": p.status, "reopen_count": p.reopen_count}


# ── 연말 결산분개 (손익→이익잉여금) ─────────────────────────────────
@router.post("/closing/{fiscal_year}")
async def close_year(fiscal_year: int,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """회계연도 결산분개 — 손익계정을 이익잉여금으로 이전. 멱등(연도별 1전표)."""
    _require(user, "write", "finance.periods")
    from erp.common.kst import kst_today
    from erp.finance.closing_service import ClosingService
    from erp.finance.fiscal import fiscal_range
    _since, until = await fiscal_range(session, fiscal_year)
    if kst_today() <= until:      # 2026-10-10 연말 대비 — 종료 전 실행은 미래일자 결산 전표(손익 이전이 중간에 끊김)
        raise ValidationError(f"{fiscal_year} 회계연도는 {until}에 끝납니다 — 회계연도가 끝난 뒤(12월 결산 마감 후) 실행하세요",
                              {"fiscal_year_end": str(until)})
    out = await ClosingService().close_year(session, fiscal_year=fiscal_year, user_id=user.user_id)
    await _money_audit(session, user, "YEAR_CLOSE", "finance.closing", fiscal_year,
                       after={"net_income": out.get("net_income")})
    await session.commit()
    return out


# ── 리스 회계 (K-IFRS 16) ─────────────────────────────────────────
def _lease_service():
    from erp.finance.lease_service import LeaseService
    return LeaseService()


class LeaseBody(BaseModel):
    lease_no: str
    asset_name: str
    start_on: date
    term_months: int
    monthly_payment: float
    annual_rate: float = 0
    lessor: str = ""
    exempt: bool = False


class LeaseRemeasureBody(BaseModel):
    new_monthly_payment: float
    new_term_months: int
    on_date: date


@router.post("/leases", status_code=201)
async def create_lease(body: LeaseBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """리스개시 — 사용권자산·리스부채 인식."""
    _require(user, "write", "finance.assets")
    lease = await _lease_service().create(session, user_id=user.user_id, **body.model_dump())
    await session.commit()
    return {"id": lease.id, "rou_initial": float(lease.rou_initial),
            "liability_initial": float(lease.liability_initial)}


@router.post("/leases/{lease_id}/accrue/{yyyymm}")
async def accrue_lease(lease_id: int, yyyymm: YearMonth,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """월별 리스 회계 — 이자·원금상환·상각 전표."""
    _require(user, "write", "finance.assets")
    out = await _lease_service().accrue_monthly(session, lease_id=lease_id, period=yyyymm,
                                                user_id=user.user_id)
    await session.commit()
    return out


@router.post("/leases/{lease_id}/remeasure")
async def remeasure_lease(lease_id: int, body: LeaseRemeasureBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """리스변경 재측정(K-IFRS 1116) — 잔여기간 신지급액 현가로 리스부채·사용권자산 조정."""
    _require(user, "write", "finance.assets")
    out = await _lease_service().remeasure(session, lease_id=lease_id, user_id=user.user_id,
                                           **body.model_dump())
    await _money_audit(session, user, "LEASE_REMEASURE", "finance.lease_contracts", lease_id,
                       after=out)
    await session.commit()
    return out


@router.get("/leases")
async def list_leases(user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """리스계약 현황 — 사용권자산 장부·리스부채 잔액."""
    _require(user, "read", "finance.assets")
    return {"items": await _lease_service().list_leases(session)}

# 월마감 일괄(month-end close)은 감가(finance)+퇴직충당(hr)+리스 크로스도메인 오케스트레이션이라
# erp.app.month_end_router로 이전(hr↔finance 순환 회피). URL은 /finance/month-end로 동일.


@router.get("/cash-forecast")
async def cash_forecast(as_of: date, months: int = 6,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """자금수지 예측 — 현금잔액 + AR 수금예정·AP 지급예정으로 월별 포지션 추정."""
    _require(user, "read", "finance.settlement")
    return await _settle.cash_forecast(session, as_of=as_of, months=months)


# ── 검진권 전기 규칙 (관리자 설정 — 수수료 방향·정산 AP/AR 등 기관별 다양) ──────
_VRULE_LINE_TYPES = {"purchase", "sub_purchase", "resale", "redeem", "topup",
                     "refund", "commission", "settlement"}


class PostingRuleBody(BaseModel):
    line_type: str
    dealer_type: str | None = None
    debit_account: str | None = None
    credit_account: str | None = None
    amount_source: str | None = None
    reverse_on_negative: bool = False
    skip: bool = False
    active: bool = True
    priority: int = 100
    description: str | None = None


def _vrule_dict(r):
    return {"id": r.id, "line_type": r.line_type, "dealer_type": r.dealer_type,
            "debit_account": r.debit_account, "credit_account": r.credit_account,
            "amount_source": r.amount_source, "reverse_on_negative": r.reverse_on_negative,
            "skip": r.skip, "active": r.active, "priority": r.priority,
            "description": r.description}


@router.get("/voucher/posting-rules")
async def list_posting_rules(user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """검진권 전기 규칙 목록 (관리자) — lineType별 차·대 계정·반전·무전표 설정."""
    _require(user, "read", "finance.vouchers")
    from erp.finance.models import VoucherPostingRule
    rows = (await session.execute(select(VoucherPostingRule).order_by(
        VoucherPostingRule.line_type, VoucherPostingRule.priority,
        VoucherPostingRule.id))).scalars().all()
    return {"items": [_vrule_dict(r) for r in rows]}


@router.post("/voucher/posting-rules", status_code=201)
async def create_posting_rule(body: PostingRuleBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """전기 규칙 신설 (관리자) — 새 처리 방식·딜러유형별 규칙 추가."""
    _require(user, "write", "finance.vouchers")
    if body.line_type not in _VRULE_LINE_TYPES:
        raise ValidationError(f"허용되지 않는 line_type: {body.line_type}")
    from erp.finance.models import VoucherPostingRule
    r = VoucherPostingRule(**body.model_dump(), updated_by=user.user_id)
    session.add(r)
    await session.flush()
    await _money_audit(session, user, "CREATE", "finance.voucher_posting_rule", r.id,
                       after=_vrule_dict(r))
    return _vrule_dict(r)


@router.put("/voucher/posting-rules/{rule_id}")
async def update_posting_rule(rule_id: int, body: PostingRuleBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """전기 규칙 수정 (관리자) — 수수료 방향·정산 계정 조정 등."""
    _require(user, "write", "finance.vouchers")
    if body.line_type not in _VRULE_LINE_TYPES:
        raise ValidationError(f"허용되지 않는 line_type: {body.line_type}")
    from erp.finance.models import VoucherPostingRule
    r = await session.get(VoucherPostingRule, rule_id)
    if r is None:
        raise ValidationError("존재하지 않는 규칙입니다")
    for k, v in body.model_dump().items():
        setattr(r, k, v)
    r.updated_by = user.user_id
    await _money_audit(session, user, "UPDATE", "finance.voucher_posting_rule", r.id,
                       after=_vrule_dict(r))
    return _vrule_dict(r)


# ── 원천징수 요율 (시점 버전관리 — 세법 개정 대응, 관리자 설정) ──────────────
class WithholdingBody(BaseModel):
    dealer_type: str | None = None
    rate_pct: float = Field(ge=0, le=100)      # 원천징수율 % — 음수·과대 방지
    effective_from: date
    effective_to: date | None = None
    withholding_account: str = "2150"
    tax_invoice_by: str | None = None
    active: bool = True
    note: str | None = None


def _wh_dict(r):
    return {"id": r.id, "dealer_type": r.dealer_type, "rate_pct": float(r.rate_pct),
            "effective_from": r.effective_from.isoformat(),
            "effective_to": r.effective_to.isoformat() if r.effective_to else None,
            "withholding_account": r.withholding_account, "tax_invoice_by": r.tax_invoice_by,
            "active": r.active, "note": r.note}


@router.get("/withholding-rates")
async def list_withholding_rates(user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """원천징수 요율 목록 (관리자) — 시행일 구간별 요율(세법 개정 이력)."""
    _require(user, "read", "finance.vouchers")
    from erp.finance.models import WithholdingRate
    rows = (await session.execute(select(WithholdingRate).order_by(
        WithholdingRate.dealer_type, WithholdingRate.effective_from.desc()))).scalars().all()
    return {"items": [_wh_dict(r) for r in rows]}


@router.post("/withholding-rates", status_code=201)
async def create_withholding_rate(body: WithholdingBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """원천징수 요율 신설 (관리자) — 세법 개정 시 새 시행일 구간 추가."""
    _require(user, "write", "finance.vouchers")
    from erp.finance.models import WithholdingRate
    r = WithholdingRate(**body.model_dump(), updated_by=user.user_id)
    session.add(r)
    await session.flush()
    await _money_audit(session, user, "CREATE", "finance.withholding_rate", r.id, after=_wh_dict(r))
    return _wh_dict(r)


@router.put("/withholding-rates/{rate_id}")
async def update_withholding_rate(rate_id: int, body: WithholdingBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """원천징수 요율 수정 (관리자) — 종료일 마감·비활성 등."""
    _require(user, "write", "finance.vouchers")
    from erp.finance.models import WithholdingRate
    r = await session.get(WithholdingRate, rate_id)
    if r is None:
        raise ValidationError("존재하지 않는 요율입니다")
    for k, v in body.model_dump().items():
        setattr(r, k, v)
    r.updated_by = user.user_id
    await _money_audit(session, user, "UPDATE", "finance.withholding_rate", r.id, after=_wh_dict(r))
    return _wh_dict(r)


# ── 법인 자금계좌 마스터 (외화 포함 — 통화별 분리 관리) ──────────────────────
_CURRENCIES = {"KRW", "USD", "EUR", "JPY", "CNY", "GBP"}


class CorpAccountBody(BaseModel):
    alias: str
    bank_name: str
    account_no: str
    currency: str = "KRW"
    gl_account_code: str
    is_default: bool = False
    is_active: bool = True
    holder: str | None = None
    swift: str | None = None
    note: str | None = None


def _mask_acct(no: str) -> str:
    return no if len(no) <= 4 else "*" * (len(no) - 4) + no[-4:]


def _corp_dict(r, *, mask: bool = True):
    return {"id": r.id, "alias": r.alias, "bank_name": r.bank_name,
            "account_no": _mask_acct(r.account_no) if mask else r.account_no,
            "currency": r.currency, "gl_account_code": r.gl_account_code,
            "is_default": r.is_default, "is_active": r.is_active,
            "holder": r.holder, "swift": r.swift, "note": r.note}


async def _validate_corp(session, body: CorpAccountBody, *, exclude_id: int | None = None):
    from erp.finance.models import Account, CorporateAccount
    if body.currency not in _CURRENCIES:
        raise ValidationError(f"허용되지 않는 통화: {body.currency}")
    acc = await session.scalar(
        select(Account.code).where(Account.code == body.gl_account_code))
    if acc is None:
        raise ValidationError(f"존재하지 않는 GL 계정코드: {body.gl_account_code}")
    dup = await session.scalar(select(CorporateAccount.id).where(
        CorporateAccount.bank_name == body.bank_name,
        CorporateAccount.account_no == body.account_no,
        CorporateAccount.id != (exclude_id or -1)))
    if dup is not None:
        raise ValidationError(f"이미 등록된 계좌입니다: {body.bank_name} {body.account_no}")
    # 통화 혼입 차단 — 기말환산(K-IFRS 1021)의 장부가는 GL 계정코드 전체 합산이라, 통화가 다른
    # 계좌가 같은 GL을 쓰면 환산차가 조용히 틀린다. 동일 통화 다계좌는 정상 실무라 허용하되
    # 환산 시 경고로 합계 입력을 유도한다(ForexTranslationService._gl_cohabitants).
    # 비활성 계좌도 대상 — 비활성화해도 그 계좌의 과거 전기는 GL 장부가에 남는다(우회 차단)
    shared = (await session.execute(select(CorporateAccount.id, CorporateAccount.currency).where(
        CorporateAccount.gl_account_code == body.gl_account_code,
        CorporateAccount.currency != body.currency,
        CorporateAccount.id != (exclude_id or -1)))).all()
    if shared:
        who = ", ".join(f"#{i}({c})" for i, c in shared)
        raise ValidationError(
            f"GL {body.gl_account_code}는 이미 다른 통화 계좌가 사용 중입니다({who}) — "
            f"장부가에 통화가 섞여 기말환산이 부정확해집니다. 통화마다 전용 GL 계정을 지정하세요.")


async def _clear_default(session, currency: str, keep_id: int | None):
    """통화별 기본 계좌는 1개 — 신규/변경 기본 지정 시 기존 기본 해제."""
    from erp.finance.models import CorporateAccount
    rows = (await session.execute(select(CorporateAccount).where(
        CorporateAccount.currency == currency, CorporateAccount.is_default.is_(True)))).scalars()
    for r in rows:
        if r.id != keep_id:
            r.is_default = False


@router.get("/corporate-accounts")
async def list_corp_accounts(reveal: bool = False,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """법인 자금계좌 목록 (관리자) — 통화별 계좌·기본계좌·GL 매핑. 계좌번호는 기본 마스킹."""
    _require(user, "read", "finance.vouchers")
    from erp.finance.models import CorporateAccount
    rows = (await session.execute(select(CorporateAccount).order_by(
        CorporateAccount.currency, CorporateAccount.id))).scalars().all()
    return {"items": [_corp_dict(r, mask=not reveal) for r in rows]}


@router.post("/corporate-accounts", status_code=201)
async def create_corp_account(body: CorpAccountBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """법인 자금계좌 신설 (관리자)."""
    _require(user, "write", "finance.vouchers")
    await _validate_corp(session, body)
    from erp.finance.models import CorporateAccount
    if body.is_default:
        await _clear_default(session, body.currency, None)
    r = CorporateAccount(**body.model_dump(), updated_by=user.user_id)
    session.add(r)
    await session.flush()
    await _money_audit(session, user, "CREATE", "finance.corporate_account", r.id,
                       after=_corp_dict(r))
    return _corp_dict(r)


@router.put("/corporate-accounts/{acct_id}")
async def update_corp_account(acct_id: int, body: CorpAccountBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """법인 자금계좌 수정 (관리자) — 통화·GL·기본·활성 등."""
    _require(user, "write", "finance.vouchers")
    await _validate_corp(session, body, exclude_id=acct_id)
    from erp.finance.models import CorporateAccount
    r = await session.get(CorporateAccount, acct_id)
    if r is None:
        raise ValidationError("존재하지 않는 계좌입니다")
    if body.is_default:
        await _clear_default(session, body.currency, acct_id)
    for k, v in body.model_dump().items():
        setattr(r, k, v)
    r.updated_by = user.user_id
    await _money_audit(session, user, "UPDATE", "finance.corporate_account", r.id,
                       after=_corp_dict(r))
    return _corp_dict(r)


# ── 계정과목(COA) 마스터 관리 (재무제표·대시보드 분류 기반) ──────
class AccountBody(BaseModel):
    code: str
    name: str
    kind: str                       # ASSET|LIABILITY|CAPITAL|REVENUE|EXPENSE
    parent_id: int | None = None
    cost_type: str | None = None    # FIXED|VARIABLE|None — 대시보드 고정비 분류
    is_current: bool | None = None  # 유동 여부(BS 분류·G8)
    cf_activity: str | None = None  # OPERATING|INVESTING|FINANCING(현금흐름·G8)
    ifrs_element: str | None = None  # 표준 IFRS concept(G10 기초)


class AccountUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    parent_id: int | None = None
    active: bool | None = None
    cost_type: str | None = None
    clear_cost_type: bool = False   # cost_type 미지정(None)으로 되돌림
    cost_function: str | None = None  # COGS|SGA|OTHER — 기능별 손익(IAS 1)
    clear_cost_function: bool = False
    is_current: bool | None = None    # 유동 여부(BS 분류·G8)
    clear_is_current: bool = False
    cf_activity: str | None = None    # OPERATING|INVESTING|FINANCING(현금흐름·G8)
    clear_cf_activity: bool = False
    ifrs_element: str | None = None   # 표준 IFRS concept(G10 기초)
    clear_ifrs_element: bool = False


@router.get("/accounts")
async def list_accounts(kind: str | None = None, cost_type: str | None = None,
                        include_inactive: bool = True,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """계정과목 목록 — 종류·원가구분(cost_type) 필터·비활성 포함. in_use=전표 사용 중 표시."""
    _require(user, "read", "finance.accounts")
    from erp.finance.account_service import AccountService
    return {"items": await AccountService().list(
        session, kind=kind, cost_type=cost_type, include_inactive=include_inactive)}


@router.get("/accounts/classification-coverage")
async def account_classification_coverage(user: CurrentUser = Depends(get_current_user),
                                          session: AsyncSession = Depends(get_session)):
    """IFRS 분류(G8) 커버리지 — is_current/cf_activity/ifrs_element 미지정(폴백 의존) 계정 감사."""
    _require(user, "read", "finance.accounts")
    from erp.finance.account_service import AccountService
    return await AccountService().classification_coverage(session)


@router.get("/accounts/statements-taxonomy")
async def statements_taxonomy(user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """G10 기초 — 계정↔IFRS concept 매핑 export(개념 태깅 재무제표 입력). ⚠DART XBRL 인스턴스 아님.

    XBRL 인스턴스 생성기·제출은 규제 확인(비영리 의료법인 DART 대상 여부) 후 별도 프로젝트."""
    _require(user, "read", "finance.statements")
    from erp.finance.account_service import AccountService
    return await AccountService().statements_taxonomy(session)


@router.post("/accounts", status_code=201)
async def create_account(body: AccountBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """계정과목 생성 (관리자) — code 유일·kind 검증."""
    _require(user, "write", "finance.accounts")
    from erp.finance.account_service import AccountService
    a = await AccountService().create(session, code=body.code, name=body.name, kind=body.kind,
                                      parent_id=body.parent_id, cost_type=body.cost_type,
                                      is_current=body.is_current, cf_activity=body.cf_activity,
                                      ifrs_element=body.ifrs_element)
    await _money_audit(session, user, "CREATE", "finance.accounts", a.id,
                       after={"code": a.code, "name": a.name, "kind": a.kind})
    return {"id": a.id, "code": a.code}


@router.put("/accounts/{account_id}")
async def update_account(account_id: int, body: AccountUpdate,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """계정과목 수정 (관리자) — 명칭·종류·상위·활성·원가구분. code는 불변(사용 중 집계 보존)."""
    _require(user, "write", "finance.accounts")
    from erp.finance.account_service import AccountService
    a = await AccountService().update(session, account_id=account_id, name=body.name,
                                      kind=body.kind, parent_id=body.parent_id, active=body.active,
                                      cost_type=body.cost_type, clear_cost_type=body.clear_cost_type,
                                      cost_function=body.cost_function,
                                      clear_cost_function=body.clear_cost_function,
                                      is_current=body.is_current, clear_is_current=body.clear_is_current,
                                      cf_activity=body.cf_activity, clear_cf_activity=body.clear_cf_activity,
                                      ifrs_element=body.ifrs_element,
                                      clear_ifrs_element=body.clear_ifrs_element)
    await _money_audit(session, user, "UPDATE", "finance.accounts", a.id,
                       after={"name": a.name, "kind": a.kind, "active": a.active,
                              "cost_type": a.cost_type})
    return {"id": a.id, "code": a.code, "active": a.active, "cost_type": a.cost_type}


# ── 차입금 관리 (자금관리 심화) ──────────────────────────────────
class LoanBody(BaseModel):
    lender: str
    principal: float
    annual_rate: float
    started_on: date
    term_months: int
    kind: str = "SHORT"


@router.post("/loans", status_code=201)
async def register_loan(body: LoanBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """차입 실행 — DR 현금/CR 차입금."""
    _require(user, "write", "finance.periods")
    loan = await _loans.register(session, lender=body.lender, principal=body.principal,
                                 annual_rate=body.annual_rate, started_on=body.started_on,
                                 term_months=body.term_months, kind=body.kind, actor=user.user_id)
    return {"id": loan.id, "status": loan.status}


@router.post("/loans/{loan_id}/accrue-interest")
async def accrue_loan_interest(loan_id: int, yyyymm: YearMonth,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """월 이자 인식 — 잔여원금×연이율/12 (멱등)."""
    _require(user, "write", "finance.periods")
    return await _loans.accrue_interest(session, loan_id=loan_id, yyyymm=yyyymm, actor=user.user_id)


class LoanRepayBody(BaseModel):
    amount: float
    on: date


@router.post("/loans/{loan_id}/repay")
async def repay_loan(loan_id: int, body: LoanRepayBody,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """원금 상환 — DR 차입금/CR 현금. 전액 상환 시 REPAID."""
    _require(user, "write", "finance.periods")
    loan = await _loans.repay(session, loan_id=loan_id, amount=body.amount, on=body.on,
                              actor=user.user_id)
    return {"id": loan.id, "status": loan.status,
            "outstanding": round(float(loan.principal) - float(loan.repaid_principal))}


@router.get("/loans")
async def list_loans(user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    _require(user, "read", "finance.periods")
    from erp.finance.models import Loan
    rows = (await session.execute(select(Loan).order_by(Loan.id.desc()))).scalars().all()
    return {"items": [{"id": loan.id, "lender": loan.lender, "kind": loan.kind,
                       "principal": float(loan.principal),
                       "outstanding": round(float(loan.principal) - float(loan.repaid_principal)),
                       "annual_rate": float(loan.annual_rate), "status": loan.status}
                      for loan in rows]}


# ── 지출·경비 관리 (Tier1·마이그 0210) — 경비청구·법인카드 ─────────────────
_expense = None


def _expense_service():
    global _expense
    if _expense is None:
        from erp.finance.expense_service import ExpenseService
        _expense = ExpenseService()
    return _expense


async def _self_employee(session: AsyncSession, user: CurrentUser) -> int | None:
    from erp.common.models import User
    return (await session.execute(
        select(User.employee_id).where(User.id == user.user_id))).scalar_one_or_none()


class ExpenseLineBody(BaseModel):
    expense_on: date
    account_code: str
    description: str
    amount: float
    merchant: str | None = None
    card_tx_id: int | None = None


class ExpenseClaimBody(BaseModel):
    title: str
    claim_on: date
    lines: list[ExpenseLineBody]
    note: str | None = None
    employee_id: int | None = None    # ALL 스코프(관리자)만 타인 명의 대리 기안 허용


@router.post("/expenses", status_code=201)
async def create_expense_claim(body: ExpenseClaimBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """경비 청구 기안(DRAFT) — SELF는 본인 명의 강제(위조 차단)·영수증은 Attachment(expense)."""
    from erp.common.policy import ScopeKind
    scope = _require(user, "write", "finance.expenses")
    self_emp = await _self_employee(session, user)
    dept_id = user.dept_id
    if scope.kind == ScopeKind.ALL and body.employee_id is not None:
        emp_id = body.employee_id
        if emp_id != self_emp:   # 대리 기안(v1.204) — 부서는 명의자 기준(관리자 부서 아님).
            # 경계: finance→hr 금지 계약이라 hr.employees 대신 common.User(employee_id 연결)로 조회.
            from erp.common.models import User
            dept_id = (await session.execute(select(User.department_id).where(
                User.employee_id == emp_id))).scalar_one_or_none() or user.dept_id
    else:
        if self_emp is None:
            raise ValidationError("직원 정보가 연결되지 않은 계정입니다")
        emp_id = self_emp
    claim = await _expense_service().create_claim(
        session, employee_id=emp_id, department_id=dept_id, title=body.title,
        claim_on=body.claim_on, note=body.note, created_by=user.user_id,
        lines=[ln.model_dump() for ln in body.lines])
    return {"id": claim.id, "total_amount": float(claim.total_amount),
            "status": claim.status}


@router.get("/expenses")
async def list_expense_claims(status: str | None = None, page: int = 1, size: int = 50,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """경비 청구 목록 — SELF=본인·DEPT=본부서·ALL=전체(행 스코프 명시 합성)."""
    from erp.common.policy import ScopeKind
    from erp.finance.models import ExpenseClaim
    scope = _require(user, "read", "finance.expenses")
    size = min(size, 200)
    q = select(ExpenseClaim)
    if status:
        q = q.where(ExpenseClaim.status == status)
    if scope.kind == ScopeKind.SELF:
        emp = await _self_employee(session, user)
        q = q.where(ExpenseClaim.employee_id == (emp if emp is not None else -1))
    elif scope.kind == ScopeKind.DEPT:
        q = q.where(ExpenseClaim.department_id == user.dept_id)
    rows = (await session.scalars(
        q.order_by(ExpenseClaim.id.desc()).offset((page - 1) * size).limit(size))).all()
    return {"items": [{"id": c.id, "employee_id": c.employee_id, "title": c.title,
                       "claim_on": c.claim_on, "total_amount": float(c.total_amount),
                       "status": c.status, "paid_on": c.paid_on} for c in rows],
            "page": page, "size": size}


async def _assert_claim_scope(session: AsyncSession, user: CurrentUser, scope, claim) -> None:
    """행 스코프 — SELF는 본인 청구·DEPT는 본부서만(IDOR)."""
    from erp.common.policy import ScopeKind
    from erp.errors import ForbiddenError
    if scope.kind == ScopeKind.SELF:
        if claim.employee_id != await _self_employee(session, user):
            raise ForbiddenError("본인 경비 청구만 접근할 수 있습니다")
    elif scope.kind == ScopeKind.DEPT and claim.department_id != user.dept_id:
        raise ForbiddenError("본인 부서 청구만 접근할 수 있습니다")


@router.get("/expenses/{claim_id}")
async def get_expense_claim(claim_id: int,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    from erp.finance.models import ExpenseLine
    scope = _require(user, "read", "finance.expenses")
    svc = _expense_service()
    c = await svc._get(session, claim_id)
    await _assert_claim_scope(session, user, scope, c)
    lines = (await session.scalars(select(ExpenseLine).where(
        ExpenseLine.claim_id == claim_id).order_by(ExpenseLine.id))).all()
    return {"id": c.id, "employee_id": c.employee_id, "department_id": c.department_id,
            "title": c.title, "note": c.note, "claim_on": c.claim_on,
            "total_amount": float(c.total_amount), "status": c.status,
            "voucher_ref": c.voucher_ref, "payment_voucher_ref": c.payment_voucher_ref,
            "paid_on": c.paid_on,
            "lines": [{"id": ln.id, "expense_on": ln.expense_on,
                       "account_code": ln.account_code, "description": ln.description,
                       "merchant": ln.merchant, "amount": float(ln.amount),
                       "card_tx_id": ln.card_tx_id} for ln in lines]}


@router.post("/expenses/{claim_id}/submit")
async def submit_expense_claim(claim_id: int,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """상신 — 본인(또는 ALL) 기안 건만·결재선 생성(EXPENSE 룰 있으면 다단계)."""
    scope = _require(user, "write", "finance.expenses")
    svc = _expense_service()
    c = await svc._get(session, claim_id)
    await _assert_claim_scope(session, user, scope, c)
    out = await svc.submit(session, claim_id=claim_id, actor=user.user_id)
    return {"id": out.id, "status": out.status}


@router.post("/expenses/{claim_id}/approve")
async def approve_expense_claim(claim_id: int, override: bool = False,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """승인 — 재무 도메인 승인권(감사 M5·IT/물류 제외)·DEPT는 본부서·완주 시 비용/미지급 전기."""
    scope = require_scope(user, "finance.expenses", "approve")
    svc = _expense_service()
    c = await svc._get(session, claim_id)
    await _assert_claim_scope(session, user, scope, c)   # DEPT 승인자는 본부서 청구만
    out = await svc.approve(session, claim_id=claim_id, actor=user.user_id, override=override)
    return {"id": out.id, "status": out.status, "voucher_ref": out.voucher_ref}


class ExpenseRejectBody(BaseModel):
    reason: str = ""


@router.post("/expenses/{claim_id}/reject")
async def reject_expense_claim(claim_id: int, body: ExpenseRejectBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    scope = require_scope(user, "finance.expenses", "approve")
    svc = _expense_service()
    c = await svc._get(session, claim_id)
    await _assert_claim_scope(session, user, scope, c)
    out = await svc.reject(session, claim_id=claim_id, actor=user.user_id, reason=body.reason)
    return {"id": out.id, "status": out.status}


class ExpensePayBody(BaseModel):
    paid_on: date


@router.post("/expenses/{claim_id}/pay")
async def pay_expense_claim(claim_id: int, body: ExpensePayBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """지급(개인분 현금전기) — ADMIN 전용(현금전기 SoD 규약)."""
    _require(user, "admin", "finance.expenses")
    return await _expense_service().pay(session, claim_id=claim_id, paid_on=body.paid_on,
                                        actor=user.user_id)


# ── 법인카드 ─────────────────────────────────────────────
class CardBody(BaseModel):
    alias: str
    card_last4: str
    holder_employee_id: int | None = None
    department_id: int | None = None
    note: str | None = None


@router.post("/corporate-cards", status_code=201)
async def register_corporate_card(body: CardBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """법인카드 등록 — 마지막 4자리만(전체 카드번호 저장 금지·PCI 최소화)."""
    _require(user, "admin", "finance.expenses")
    card = await _expense_service().register_card(session, **body.model_dump())
    return {"id": card.id, "alias": card.alias}


def _card_scope_filter(q, scope, user: CurrentUser, self_emp: int | None):
    """카드 행 스코프 — SELF=본인 보유+본부서 공용(무소지자)·DEPT=본부서·ALL=전체.

    적대검토(v1.203): read 게이트만 있고 SELF/DEPT kind를 폐기해 전 직원급이 전 카드·거래를
    열람하던 결함 — /expenses 목록과 동일한 명시 합성으로 통일."""
    from erp.common.policy import ScopeKind
    from erp.finance.models import CorporateCard
    if scope.kind == ScopeKind.SELF:
        return q.where(
            (CorporateCard.holder_employee_id == (self_emp if self_emp is not None else -1))
            | (CorporateCard.holder_employee_id.is_(None)
               & (CorporateCard.department_id == user.dept_id)))
    if scope.kind == ScopeKind.DEPT:
        return q.where(CorporateCard.department_id == user.dept_id)
    return q


@router.get("/corporate-cards")
async def list_corporate_cards(user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    from erp.finance.models import CorporateCard
    scope = _require(user, "read", "finance.expenses")
    q = _card_scope_filter(select(CorporateCard), scope, user,
                           await _self_employee(session, user))
    rows = (await session.scalars(q.order_by(CorporateCard.id))).all()
    return {"items": [{"id": c.id, "alias": c.alias, "card_last4": c.card_last4,
                       "holder_employee_id": c.holder_employee_id,
                       "department_id": c.department_id, "is_active": c.is_active}
                      for c in rows]}


class CardTxRow(BaseModel):
    tx_id: str
    tx_on: date
    amount: float
    merchant: str | None = None


@router.post("/corporate-cards/{card_id}/transactions")
async def import_card_transactions(card_id: int, rows: list[CardTxRow],
                                   user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """사용내역 bulk 적재 — 승인번호(tx_id) 멱등(중복 스킵)."""
    _require(user, "admin", "finance.expenses")
    return await _expense_service().import_transactions(
        session, card_id=card_id, rows=[r.model_dump() for r in rows])


@router.get("/corporate-cards/{card_id}/transactions")
async def list_card_transactions(card_id: int, status: str | None = None,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    from erp.errors import ForbiddenError
    from erp.finance.models import CardTransaction, CorporateCard
    scope = _require(user, "read", "finance.expenses")
    # 행 스코프(v1.203) — 대상 카드가 내 스코프에 들어와야 거래 열람 가능(카드 EP 스코프 소거 보완)
    card_q = _card_scope_filter(select(CorporateCard.id).where(CorporateCard.id == card_id),
                                scope, user, await _self_employee(session, user))
    if await session.scalar(card_q) is None:
        raise ForbiddenError("접근 가능한 법인카드가 아닙니다")
    q = select(CardTransaction).where(CardTransaction.card_id == card_id)
    if status:
        q = q.where(CardTransaction.status == status)
    rows = (await session.scalars(q.order_by(CardTransaction.tx_on.desc()).limit(500))).all()
    return {"items": [{"id": t.id, "tx_id": t.tx_id, "tx_on": t.tx_on,
                       "merchant": t.merchant, "amount": float(t.amount),
                       "status": t.status, "claim_id": t.claim_id,
                       "settled_on": t.settled_on} for t in rows]}


class CardSettleBody(BaseModel):
    yyyymm: YearMonth
    paid_on: date


@router.post("/corporate-cards/{card_id}/settle")
async def settle_corporate_card(card_id: int, body: CardSettleBody,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """카드사 월정산 — 승인된 청구 귀속 내역 합계 DR 2112/CR 1110·SETTLED(ADMIN 전용)."""
    _require(user, "admin", "finance.expenses")
    return await _expense_service().settle_card_month(
        session, card_id=card_id, yyyymm=body.yyyymm, paid_on=body.paid_on,
        actor=user.user_id)


# ── 부대사업 수익(의료외수익·마이그 0215) ─────────────────────
_ancillary = None


def _ancillary_service():
    global _ancillary
    if _ancillary is None:
        from erp.finance.ancillary_service import AncillaryRevenueService
        _ancillary = AncillaryRevenueService()
    return _ancillary


class AncillaryItemBody(BaseModel):
    name: str
    category: str                  # PARKING|RENTAL|CAFETERIA|CHECKUP|FUNERAL|OTHER
    taxable: bool = True
    default_price: float | None = None
    account_code: str = "4130"
    note: str | None = None


@router.post("/ancillary/items", status_code=201)
async def create_ancillary_item(body: AncillaryItemBody,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """의료외수익 항목 등록 — 기관 실제 부대사업만 활성화."""
    _require(user, "write", "finance.ancillary")
    item = await _ancillary_service().create_item(session, **body.model_dump())
    await session.commit()
    return {"id": item.id, "name": item.name, "category": item.category}


@router.get("/ancillary/items")
async def list_ancillary_items(active_only: bool = False,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    _require(user, "read", "finance.ancillary")
    return {"items": await _ancillary_service().list_items(session, active_only=active_only)}


@router.patch("/ancillary/items/{item_id}")
async def toggle_ancillary_item(item_id: int, active: bool,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    _require(user, "write", "finance.ancillary")
    item = await _ancillary_service().set_item_active(session, item_id=item_id, active=active)
    await session.commit()
    return {"id": item.id, "active": item.active}


class AncillaryRevenueBody(BaseModel):
    item_id: int
    amount: float
    method: str = "CASH"
    occurred_on: date
    qty: float = 1
    note: str | None = None
    client_ref: str | None = None   # 멱등키(F5·재전송 디둡)


@router.post("/ancillary/revenues", status_code=201)
async def record_ancillary_revenue(body: AncillaryRevenueBody,
                                   user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """의료외수익 수납 — 과세 항목은 순액(4130)+매출세액(2135) 분리 전기."""
    _require(user, "write", "finance.ancillary")
    rev = await _ancillary_service().record(
        session, item_id=body.item_id, amount=body.amount, method=body.method,
        occurred_on=body.occurred_on, qty=body.qty, note=body.note, actor=user.user_id,
        client_ref=body.client_ref)
    await session.commit()
    return {"id": rev.id, "net_amount": float(rev.net_amount),
            "vat_amount": float(rev.vat_amount), "voucher_ref": rev.voucher_ref}


@router.get("/ancillary/summary")
async def ancillary_summary(since: date, until: date,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """기간 의료외수익 카테고리별 집계(부가세 신고 보조)."""
    _require(user, "read", "finance.ancillary")
    return await _ancillary_service().summary(session, since=since, until=until)


# ── 고정자산 부서이동·실사(재물조사) (마이그 0218) ─────────────
_asset_ops = None


def _asset_ops_service():
    global _asset_ops
    if _asset_ops is None:
        from erp.finance.asset_ops_service import AssetOpsService
        _asset_ops = AssetOpsService()
    return _asset_ops


class AssetTransferBody(BaseModel):
    to_dept: int | None = None
    transferred_on: date
    reason: str | None = None


@router.post("/assets/{asset_id}/transfer")
async def transfer_asset(asset_id: int, body: AssetTransferBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """고정자산 부서 이동 — 이력 기록(감사추적)·department_id 갱신."""
    _require(user, "write", "finance.assets")
    tr = await _asset_ops_service().transfer(
        session, asset_id=asset_id, to_dept=body.to_dept,
        transferred_on=body.transferred_on, reason=body.reason, actor=user.user_id)
    await session.commit()
    return {"id": tr.id, "from_dept": tr.from_dept, "to_dept": tr.to_dept}


@router.get("/assets/{asset_id}/transfers")
async def asset_transfer_history(asset_id: int,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    _require(user, "read", "finance.assets")
    return {"items": await _asset_ops_service().transfer_history(session, asset_id=asset_id)}


class AssetInventoryBody(BaseModel):
    name: str
    conducted_on: date
    note: str | None = None


@router.post("/asset-inventories", status_code=201)
async def open_asset_inventory(body: AssetInventoryBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """실사(재물조사) 개시 — 사용 중 자산 전체 스냅샷."""
    _require(user, "write", "finance.assets")
    inv = await _asset_ops_service().open_inventory(
        session, name=body.name, conducted_on=body.conducted_on, note=body.note,
        actor=user.user_id)
    await session.commit()
    return {"id": inv.id, "name": inv.name, "status": inv.status}


@router.get("/asset-inventories")
async def list_asset_inventories(user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    _require(user, "read", "finance.assets")
    return {"items": await _asset_ops_service().list_inventories(session)}


@router.get("/asset-inventories/{inventory_id}")
async def asset_inventory_detail(inventory_id: int, discrepancy_only: bool = False,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    _require(user, "read", "finance.assets")
    return await _asset_ops_service().inventory_detail(
        session, inventory_id=inventory_id, discrepancy_only=discrepancy_only)


class InventoryCheckBody(BaseModel):
    found_status: str              # FOUND | NOT_FOUND | RELOCATED
    actual_dept: int | None = None
    note: str | None = None


@router.post("/asset-inventory-lines/{line_id}/check")
async def check_asset_inventory_line(line_id: int, body: InventoryCheckBody,
                                     user: CurrentUser = Depends(get_current_user),
                                     session: AsyncSession = Depends(get_session)):
    """실사 라인 확인 — 정위치/소재불명/이전."""
    _require(user, "write", "finance.assets")
    line = await _asset_ops_service().check_line(
        session, line_id=line_id, found_status=body.found_status,
        actual_dept=body.actual_dept, note=body.note, actor=user.user_id)
    await session.commit()
    return {"id": line.id, "found_status": line.found_status}


@router.post("/asset-inventories/{inventory_id}/close")
async def close_asset_inventory(inventory_id: int,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """실사 마감 — 결과 집계(정위치/소재불명/이전/미확인)."""
    _require(user, "write", "finance.assets")
    out = await _asset_ops_service().close_inventory(
        session, inventory_id=inventory_id, actor=user.user_id)
    await session.commit()
    return out
