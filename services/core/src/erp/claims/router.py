"""Claims 라우터 — 업로드(§6.7)·이의신청(FR-40)·통계(FR-48)."""
import math
from datetime import date, datetime

from fastapi import APIRouter, Depends, UploadFile
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.claims.models import (
    Appeal, BundlingRule, ClaimBatch, DurRule, DxProcedureRule,
)
from erp.claims.service import (
    AppealService, ClaimPaymentService, ClaimResubmissionService, ClaimStatsService,
    ClaimSubmissionService, NoticeUploadService,
)
from erp.common.policy import ScopeKind, require_scope
from erp.common.ratelimit import SlidingWindowLimiter
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.errors import ValidationError

router = APIRouter(prefix="/claims", tags=["claims"])
_upload, _appeals, _stats = NoticeUploadService(), AppealService(), ClaimStatsService()
_payments = ClaimPaymentService()
_review_limiter = SlidingWindowLimiter()   # ai-review 사용자별 rate-limit(전 환자테이블 로드×적발 DoS 방어)
_resubmit = ClaimResubmissionService()
_submissions = ClaimSubmissionService()
_audit = AuditService()


def _require(user: CurrentUser, action: str = "read"):
    """단일 권한 게이트 위임 (Check#4 — M5 수렴)."""
    return require_scope(user, "claims.review", action)


async def _user_dept_codes(session: AsyncSession, user: CurrentUser) -> list[str] | None:
    """사용자 부서의 dept_code 매칭 후보(부서명·진료과코드) — case.dept_code와 대조용.

    ALL 스코프(ADMIN·DIRECTOR)는 None(경계 없음). DEPT 스코프(MANAGER)는 본인 dept_id를
    common.departments로 해소해 **부서명·코드** 둘 다 후보로 반환(case.dept_code는 HIS가
    부서명("내과") 또는 코드("GS")로 보냄 — 양쪽 수용). dept 미상/미해소는 빈 리스트(아무것도
    매칭 안 함 = 과차단, 보안 기본값). dept_id↔dept_code 체계 불일치(정수PK vs 부서명) 해소.
    """
    scope = require_scope(user, "claims.review", "read")
    if scope.kind == ScopeKind.ALL:
        return None
    from erp.common.dept_scope import dept_code_candidates
    return await dept_code_candidates(session, user.dept_id)


async def _assert_case_dept(session: AsyncSession, user: CurrentUser, case_id: int) -> None:
    """케이스 부서 경계 (보안 IDOR) — DEPT 스코프는 본부서 케이스만 접근.

    ALL 스코프(ADMIN·DIRECTOR)는 통과. DEPT 스코프는 케이스 dept_code가 본인 부서명/코드와
    일치할 때만 허용 — dept_code 미상(None) 케이스도 차단(보안 기본값: 부서 판정 불가 → 거부).
    case_id 미존재는 핸들러의 존재 검증에 위임.
    """
    from erp.errors import ForbiddenError
    codes = await _user_dept_codes(session, user)
    if codes is None:    # ALL 스코프
        return
    from erp.claims.models import PreReviewCase
    case = await session.get(PreReviewCase, case_id)
    if case is None:
        return
    if case.dept_code is None or case.dept_code not in codes:
        raise ForbiddenError("타 부서 심사 케이스 접근 불가")


async def _assert_query_dept(session: AsyncSession, user: CurrentUser, query_id: int) -> None:
    """질의 부서 경계 — 질의가 속한 케이스의 dept 경계로 위임 (IDOR)."""
    from erp.claims.models import PreReviewQuery
    q = await session.get(PreReviewQuery, query_id)
    if q is not None:
        await _assert_case_dept(session, user, q.case_id)


async def _assert_finding_dept(session: AsyncSession, user: CurrentUser, finding_id: int) -> None:
    """적발 부서 경계 — 적발이 속한 케이스의 dept 경계로 위임 (IDOR, 삭감 우회 차단)."""
    from erp.claims.models import PreReviewFinding
    f = await session.get(PreReviewFinding, finding_id)
    if f is not None:
        await _assert_case_dept(session, user, f.case_id)


async def _assert_adjustment_dept(session: AsyncSession, user: CurrentUser,
                                  adjustment_id: int) -> None:
    """삭감 조정 부서 경계 — 조정→명세서 dept_code로 판정 (IDOR, 이의신청 개설 우회 차단).

    ALL 스코프 통과. DEPT 스코프는 명세서 dept_code가 본부서일 때만 — 미상(None)도 차단(보안
    기본값). 조정 미존재는 핸들러 검증에 위임(존재 시에만 경계 강제).
    """
    from erp.errors import ForbiddenError
    codes = await _user_dept_codes(session, user)
    if codes is None:
        return
    from erp.claims.models import ClaimAdjustment, ClaimStatement
    row = (await session.execute(
        select(ClaimStatement.dept_code)
        .join(ClaimAdjustment, ClaimAdjustment.statement_id == ClaimStatement.id)
        .where(ClaimAdjustment.id == adjustment_id))).first()
    if row is None:      # 조정 미존재 — 핸들러가 검증
        return
    if row[0] is None or row[0] not in codes:
        raise ForbiddenError("타 부서 삭감 조정 접근 불가")


async def _assert_appeal_dept(session: AsyncSession, user: CurrentUser, appeal_id: int) -> None:
    """이의신청 부서 경계 — 이의신청→조정→명세서 dept_code로 판정 (IDOR, 손실전표 무단생성 차단)."""
    from erp.errors import ForbiddenError
    codes = await _user_dept_codes(session, user)
    if codes is None:
        return
    from erp.claims.models import ClaimAdjustment, ClaimStatement
    row = (await session.execute(
        select(ClaimStatement.dept_code)
        .join(ClaimAdjustment, ClaimAdjustment.statement_id == ClaimStatement.id)
        .join(Appeal, Appeal.adjustment_id == ClaimAdjustment.id)
        .where(Appeal.id == appeal_id))).first()
    if row is None:
        return
    if row[0] is None or row[0] not in codes:
        raise ForbiddenError("타 부서 이의신청 접근 불가")


async def _assert_chart_dept(session: AsyncSession, user: CurrentUser, *, chart_no: str,
                             care_month: str | None, encounter_id: str | None) -> None:
    """환자 진료내역 부서 경계 — 산정 대상 BillingLine의 dept_code로 판정 (IDOR).

    2026-10-09 시나리오 S-B(보안 레인): `GET /claims/calc/from-chart`만 `_require(read)`뿐이라 2과 MANAGER가
    타 부서 환자의 산정 전체(라인·금액)를 받았다 — 같은 환자를 사전심사 목록에선 0건/상세는 403으로 막는데
    산정 경로로 우회됐다. 케이스 경계와 같은 기본값: ALL 스코프 통과, DEPT 스코프는 범위 내 **모든** 라인이
    본부서여야 허용(dept_code 미상·타 부서 라인이 하나라도 있으면 거부 — 판정 불가 → 차단).
    """
    from erp.errors import ForbiddenError
    codes = await _user_dept_codes(session, user)
    if codes is None:
        return
    from erp.patient.models import BillingLine
    q = select(BillingLine.dept_code).where(BillingLine.chart_no == chart_no,
                                            BillingLine.cancelled.is_(False)).distinct()
    if encounter_id:
        q = q.where(BillingLine.encounter_id == encounter_id)
    if care_month:
        try:
            y, mo = int(care_month[:4]), int(care_month[5:7])
            start, nxt = date(y, mo, 1), date(y + (mo == 12), (mo % 12) + 1, 1)
        except ValueError as exc:
            raise ValidationError("진료월(care_month)은 YYYY-MM 형식이어야 합니다") from exc
        q = q.where(BillingLine.treated_on >= start, BillingLine.treated_on < nxt)
    depts = set((await session.scalars(q)).all())
    if any(d is None or d not in codes for d in depts):
        raise ForbiddenError("타 부서 진료내역 산정 접근 불가")


async def _assert_learning_dept(session: AsyncSession, user: CurrentUser, case_id: int) -> None:
    """학습 사례 부서 경계 — dept_code로 판정 (IDOR/PII, 타부서 학습 사례 개변 차단).

    dept_code 미상(NULL 레거시) 학습 사례는 DEPT 스코프 차단(ADMIN만 개변) — 보안 기본값.
    """
    from erp.errors import ForbiddenError
    codes = await _user_dept_codes(session, user)
    if codes is None:
        return
    from erp.claims.models import PreReviewLearningCase
    lc = await session.get(PreReviewLearningCase, case_id)
    if lc is None:      # 미존재 — 서비스가 검증
        return
    if lc.dept_code is None or lc.dept_code not in codes:
        raise ForbiddenError("타 부서 학습 사례 접근 불가")




@router.post("/upload", status_code=201)
async def upload_notice(file: UploadFile,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """통보서 파일 업로드 — fallback 필수 경로 (FR-38). 파싱은 G1 해소 후."""
    _require(user, "write")
    content = await file.read()
    staging = await _upload.upload(session, actor=user.user_id,
                                   original_name=file.filename or "unnamed", content=content)
    return {"staging_id": staging.id, "status": staging.status,
            "note": "파싱은 레이아웃 규격·실물 샘플 확보(M0c) 후 활성화됩니다 (G1)"}


@router.post("/ingest/medrlt-json", status_code=201)
async def ingest_medrlt(payload: dict,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """MEDRLT JSON 적재 — 합성 샘플 E2E·JSON 채널 (receipt_no 멱등, 실물 KEDI는 파서 경로)."""
    from erp.claims.ingest_service import ingest_medrlt_json
    _require(user, "write")
    out = await ingest_medrlt_json(session, payload=payload, actor=user.user_id)
    await _audit.record_audit(session, actor=user.user_id, action="CREATE",
                              entity="claims.claim_batches",
                              target_id=str(out.get("batch_id")),
                              after={"source": "medrlt-json",
                                     "synthetic": out.get("synthetic")})
    return out


@router.get("/batches")
async def list_batches(user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session),
                       status: str | None = None, unpaid: bool = False,
                       care_month: str | None = None, page: int = 1, size: int = 50):
    """청구 배치 목록 — 지급 생애주기 status(DECIDED|PARTIALLY_PAID|PAID) 필터·미지급(unpaid) 조회."""
    _require(user, "read")
    size = min(size, 200)
    q = select(ClaimBatch).order_by(ClaimBatch.id.desc())
    if status:
        q = q.where(ClaimBatch.status == status)
    if unpaid:   # 미지급 청구 조회 — 결정 후 미지급/부분지급
        q = q.where(ClaimBatch.status.in_(["DECIDED", "PARTIALLY_PAID"]))
    if care_month:
        q = q.where(ClaimBatch.care_month == care_month)
    rows = (await session.execute(
        q.offset((page - 1) * size).limit(size))).scalars().all()
    return {"items": [{"id": b.id, "receipt_no": b.receipt_no, "care_month": b.care_month,
                       "status": b.status, "claimed_amt": float(b.claimed_amt or 0),
                       "decided_amt": float(b.decided_amt or 0)} for b in rows],
            "page": page, "size": size}


@router.get("/statements")
async def list_statements(batch_id: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session),
                          page: int = 1, size: int = 100):
    """명세서(명일련) + 조정 내역 드릴다운 (design §7.1)."""
    _require(user, "read")
    from erp.claims.models import ClaimAdjustment, ClaimStatement
    size = min(size, 500)
    codes = await _user_dept_codes(session, user)   # IDOR — 명세서(chart_no PII) 부서 경계
    q = select(ClaimStatement).where(ClaimStatement.batch_id == batch_id)
    if codes is not None:   # DEPT 스코프 — 본부서 명세서만(dept_code 미상은 과차단=보안 기본값)
        q = q.where(ClaimStatement.dept_code.in_(codes))
    stmts = (await session.execute(
        q.order_by(ClaimStatement.stmt_serial)
        .offset((page - 1) * size).limit(size))).scalars().all()
    # 분석 M1 — N+1 제거: 조정 내역 IN-batch + 그룹핑
    adj_by_stmt: dict[int, list] = {}
    if stmts:
        for a in (await session.execute(
                select(ClaimAdjustment).where(
                    ClaimAdjustment.statement_id.in_([s.id for s in stmts])))).scalars():
            adj_by_stmt.setdefault(a.statement_id, []).append(a)
    # 분석 M6 — chart_no 노출 경로 접속기록
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="claims.claim_statements", target_id=str(batch_id),
                               route="/claims/statements")
    items = []
    for s in stmts:
        adjustments = adj_by_stmt.get(s.id, [])
        items.append({
            "id": s.id, "stmt_serial": s.stmt_serial, "chart_no": s.chart_no,
            "dept_code": s.dept_code, "doctor_id": s.doctor_id,
            "claimed_amt": float(s.claimed_amt or 0),
            "adjusted_amt": float(s.adjusted_amt or 0),
            "decided_amt": float(s.decided_amt or 0),
            "adjustments": [{"id": a.id, "adj_code": a.adj_code,
                             "adj_amt": float(a.adj_amt), "adj_reason": a.adj_reason,
                             "map_status": a.map_status} for a in adjustments]})
    return {"items": items}


class AppealOpen(BaseModel):
    adjustment_id: int
    appeal_type: str          # REVIEW_REQUEST | OBJECTION
    notice_arrived_on: date


@router.post("/appeals", status_code=201)
async def open_appeal(body: AppealOpen,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    await _assert_adjustment_dept(session, user, body.adjustment_id)   # IDOR — 타부서 삭감건 차단
    appeal = await _appeals.open(session, actor=user.user_id, **body.model_dump())
    return {"id": appeal.id, "legal_deadline": appeal.legal_deadline}


class AppealTransition(BaseModel):
    status: str               # SUPPLEMENT | REJECTED | ACCEPTED
    final_loss_amt: float | None = None


@router.patch("/appeals/{appeal_id}/status")
async def transition_appeal(appeal_id: int, body: AppealTransition,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    await _assert_appeal_dept(session, user, appeal_id)   # IDOR — 타부서 이의신청 손실전표 무단생성 차단
    appeal = await _appeals.transition(session, appeal_id=appeal_id, actor=user.user_id,
                                       **body.model_dump())
    return {"id": appeal.id, "status": appeal.status, "voucher_ref": appeal.voucher_ref}


@router.get("/appeals")
async def list_appeals(user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session),
                       page: int = 1, size: int = 50):
    _require(user, "read")
    size = min(size, 200)
    codes = await _user_dept_codes(session, user)   # IDOR — 이의신청 목록 부서 경계
    q = select(Appeal).order_by(Appeal.legal_deadline)
    if codes is not None:
        from erp.claims.models import ClaimAdjustment, ClaimStatement
        q = (q.join(ClaimAdjustment, ClaimAdjustment.id == Appeal.adjustment_id)
              .join(ClaimStatement, ClaimStatement.id == ClaimAdjustment.statement_id)
              .where(ClaimStatement.dept_code.in_(codes)))
    rows = (await session.execute(
        q.offset((page - 1) * size).limit(size))).scalars().all()
    today = date.today()
    return {"items": [{"id": a.id, "status": a.status, "legal_deadline": a.legal_deadline,
                       "d_day": (a.legal_deadline - today).days} for a in rows],
            "page": page, "size": size}


class ClaimPaymentBody(BaseModel):
    care_month: str                     # YYYY-MM
    payment_ref: str                    # 지급명세 식별(멱등 사업키)
    paid_on: date
    paid_amt: float
    note: str | None = None


@router.post("/payments", status_code=201)
async def record_claim_payment(body: ClaimPaymentBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """공단 지급 대사 — 입금(DR 1110/CR 1140) 전기 + 결정액 대비 variance. 지급명세 멱등.

    기관 단위 GL 현금전기+배치 상태 일괄변경이라 claims.review(MANAGER DEPT)가 아닌
    전용 정책(claims.payments — ADMIN 전용)으로 게이팅. 조회는 read 그대로.
    """
    require_scope(user, "claims.payments", "write")
    pay = await _payments.record(
        session, actor=user.user_id, care_month=body.care_month,
        payment_ref=body.payment_ref, paid_on=body.paid_on, paid_amt=body.paid_amt,
        note=body.note)
    return {"id": pay.id, "care_month": pay.care_month, "paid_amt": float(pay.paid_amt),
            "expected_amt": float(pay.expected_amt) if pay.expected_amt is not None else None,
            "variance": float(pay.variance) if pay.variance is not None else None,
            "voucher_ref": pay.voucher_ref}


@router.get("/payments")
async def list_claim_payments(care_month: str | None = None,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """공단 지급 대사 내역 — 진료월 필터."""
    _require(user, "read")
    return {"items": await _payments.list_payments(session, care_month=care_month)}


@router.get("/payments/reconciliation")
async def claim_payment_reconciliation(care_month: str,
                                       user: CurrentUser = Depends(get_current_user),
                                       session: AsyncSession = Depends(get_session)):
    """진료월 청구·결정·지급 대사 요약 — 미수(결정−지급)·삭감(청구−결정)."""
    _require(user, "read")
    return await _payments.reconciliation(session, care_month=care_month)


class ClaimReturnBody(BaseModel):
    original_ref: str                   # 원 청구 식별(멱등 사업키)
    care_month: str
    returned_on: date
    return_code: str | None = None
    return_reason: str | None = None
    amount: float | None = None
    note: str | None = None


@router.post("/resubmissions", status_code=201)
async def record_claim_return(body: ClaimReturnBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """반송(반려) 접수 — 재청구 추적 시작. original_ref 멱등."""
    _require(user, "write")
    r = await _resubmit.record_return(
        session, actor=user.user_id, original_ref=body.original_ref, care_month=body.care_month,
        returned_on=body.returned_on, return_reason=body.return_reason,
        return_code=body.return_code, amount=body.amount, note=body.note)
    return {"id": r.id, "status": r.status, "original_ref": r.original_ref}


class ClaimResubmitBody(BaseModel):
    resubmit_ref: str                   # 재청구 접수번호
    resubmitted_on: date


@router.patch("/resubmissions/{resubmission_id}/resubmit")
async def resubmit_claim(resubmission_id: int, body: ClaimResubmitBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """재청구 연결 — RETURNED→RESUBMITTED."""
    _require(user, "write")
    r = await _resubmit.resubmit(session, actor=user.user_id, resubmission_id=resubmission_id,
                                 resubmit_ref=body.resubmit_ref, resubmitted_on=body.resubmitted_on)
    return {"id": r.id, "status": r.status, "resubmit_ref": r.resubmit_ref}


@router.patch("/resubmissions/{resubmission_id}/close")
async def close_resubmission(resubmission_id: int, note: str | None = None,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """반송/재청구 추적 종결 — CLOSED."""
    _require(user, "write")
    r = await _resubmit.close(session, actor=user.user_id, resubmission_id=resubmission_id,
                              note=note)
    return {"id": r.id, "status": r.status}


@router.get("/resubmissions")
async def list_resubmissions(status: str | None = None, pending: bool = False,
                             care_month: str | None = None,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """반송/재청구 목록 — pending=미재청구(RETURNED)만 + 미재청구 총액."""
    _require(user, "read")
    return await _resubmit.list_resubmissions(session, status=status, pending=pending,
                                              care_month=care_month)


@router.get("/submission-reconciliation")
async def claim_submission_reconciliation(care_month: str,
                                          user: CurrentUser = Depends(get_current_user),
                                          session: AsyncSession = Depends(get_session)):
    """청구액 이중소스 정합 — 제출액(claim.submitted) vs 심사대상 청구액(MEDRLT)·variance."""
    _require(user, "read")
    return await _submissions.reconciliation(session, care_month=care_month)


@router.get("/submissions/unlinked")
async def claim_submissions_unlinked(user: CurrentUser = Depends(get_current_user),
                                     session: AsyncSession = Depends(get_session)):
    """진료월 미동봉 제출 청구 — 대사 불가분 표면화(HIS careMonth 동봉 요청 근거)."""
    _require(user, "read")
    return await _submissions.unlinked_submissions(session)


@router.get("/stats")
async def claim_stats(by: str = "dept",
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    # 부서장 scope — dept_code 후보(부서명/코드)로 집계 경계(§6.2). 기존 str(dept_id)(정수PK)는
    # dept_code(String 코드)와 매칭 불가라 항상 빈 통계(과차단)였음 → _user_dept_codes로 교정.
    codes = await _user_dept_codes(session, user)
    rows = await _stats.by_dimension(session, by=by, scope_dept_codes=codes)
    rate = await _stats.automation_rate(session)
    return {"by": by, "items": rows, "automation": rate}


# ── 사전심사 룰엔진 (FR-41) ────────────────────────────────
class PreReviewLine(BaseModel):
    chart_no: str
    code: str
    code_type: str = "TARIFF"
    count: int = 1
    amount: float | None = None
    dept_code: str | None = None        # 부서 경계(IDOR 가드)용 — 케이스 dept 결정
    treated_on: date | None = None
    # Tier1 룰 선택 입력 (PR-A): 연령/성별 제한·가산 시각 판정용
    age: int | None = None
    sex: str | None = None              # M | F
    treated_at: datetime | None = None  # 진료시각 (야간·공휴 가산 검증)


class PreReviewBody(BaseModel):
    lines: list[PreReviewLine]
    save: bool = False                  # true → 워크리스트 케이스로 영속


@router.post("/pre-review")
async def pre_review(body: PreReviewBody,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """청구 전 사전심사 — 활성 룰로 라인 평가, 삭감 위험 사전 적발 (FR-41)."""
    from datetime import date as _date

    from erp.claims.models import PreReviewRule
    from erp.claims.pre_review import PreReviewEngine
    from erp.claims.worklist_service import resolve_fee_with_mapping

    _require(user, "read")
    # 청구 코드만 진료일(as_of) 시점 유효 단가 조회 — 전량 로드 회피(실 마스터 44만건).
    # 약품 매핑(CONFIRMED) 적용 — 매핑된 HIS 코드를 미등재로 오탐하지 않도록 sync와 동일 헬퍼.
    codes = {ln.code for ln in body.lines if ln.code}
    as_of = max((ln.treated_on for ln in body.lines if ln.treated_on), default=_date.today())
    rules = (await session.execute(
        select(PreReviewRule).where(
            PreReviewRule.active.is_(True),
            or_(PreReviewRule.valid_from.is_(None), PreReviewRule.valid_from <= as_of),
            or_(PreReviewRule.valid_to.is_(None), PreReviewRule.valid_to >= as_of)))).scalars().all()
    fee = await resolve_fee_with_mapping(session, codes, as_of)
    from erp.common.holidays import HolidayService
    fee["_extra_holidays"] = await HolidayService().dates(session)   # 공휴가산 DB 캘린더(M-8)
    line_dicts = [ln.model_dump() for ln in body.lines]
    findings = PreReviewEngine().evaluate(lines=line_dicts, rules=rules, fee=fee)
    out = {"count": len(findings),
           "blocked": any(f.severity == "BLOCK" for f in findings),
           "findings": [{"rule_code": f.rule_code, "rule_name": f.rule_name,
                         "severity": f.severity, "chart_no": f.chart_no,
                         "code": f.code, "detail": f.detail} for f in findings]}
    if body.save:                       # 워크리스트 영속 (PR-A)
        from erp.claims.worklist_service import WorklistService
        _require(user, "write")
        out["worklist"] = await WorklistService().save_run(
            session, actor=user.user_id, lines=line_dicts, findings=findings)
    return out


# ── 심사 워크리스트 (PR-A) ───────────────────────────────
_worklist_svc = None


def _worklist():
    global _worklist_svc
    if _worklist_svc is None:
        from erp.claims.worklist_service import WorklistService
        _worklist_svc = WorklistService()
    return _worklist_svc


class WorklistSearchBody(BaseModel):
    status: str | None = None
    assignee_id: int | None = None
    severity: str | None = None
    chart_no: str | None = None
    encounter_type: str | None = None
    treated_on: date | None = None


@router.post("/pre-review/cases/search")
async def worklist_cases_post(body: WorklistSearchBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """심사 워크리스트 검색(POST) — Phase0 W1: chart_no가 쿼리스트링 access log에 남던
    GET을 대체(FE 전환·GET은 호환 유지)."""
    _require(user, "read")
    items = await _worklist().list_cases(
        session, status=body.status, assignee_id=body.assignee_id, severity=body.severity,
        chart_no=body.chart_no, encounter_type=body.encounter_type, treated_on=body.treated_on,
        dept_codes=await _user_dept_codes(session, user))
    return {"count": len(items), "items": items}


@router.get("/pre-review/cases")
async def worklist_cases(status: str | None = None, assignee_id: int | None = None,
                         severity: str | None = None, chart_no: str | None = None,
                         encounter_type: str | None = None, treated_on: date | None = None,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """(호환) 심사 워크리스트 — 신규 소비는 POST .../cases/search(W1·쿼리스트링 로그)."""
    _require(user, "read")
    items = await _worklist().list_cases(session, status=status, assignee_id=assignee_id,
                                         severity=severity, chart_no=chart_no,
                                         encounter_type=encounter_type, treated_on=treated_on,
                                         dept_codes=await _user_dept_codes(session, user))
    return {"count": len(items), "items": items}


@router.get("/pre-review/cases/{case_id}")
async def worklist_case_detail(case_id: int,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    await _assert_case_dept(session, user, case_id)   # IDOR — 부서 경계
    return await _worklist().detail(session, case_id=case_id)


@router.get("/pre-review/cases/{case_id}/statement")
async def worklist_case_statement(case_id: int,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """진료비 명세 (P1b) — 라인별 계산 분해·본인부담 구성·codeType 그룹·적발 연결."""
    _require(user, "read")
    await _assert_case_dept(session, user, case_id)   # IDOR — 부서 경계
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="claims.case_statement", target_id=str(case_id),
                               route="/claims/pre-review/cases/statement")
    return await _worklist().case_statement(session, case_id=case_id)


_FP_RISK_LOW = {"low", "none", "낮음", "없음", "safe", "low_risk", "no_risk", "minimal"}
_AI_SAFE_KEYS = ("risk_level", "recommendation", "evidence_citations")  # weve 응답 중 UI 투영 허용분


_FP_MEDIUM_THRESHOLD = 0.4   # LOW 밴드 상한 — ERP 소유 상수. 외부(weve) 응답으로 덮어쓰지 않음.


def _fp_candidate(out: dict) -> bool:
    """A3 — AI가 삭감위험 낮음으로 본 적발 = 오탐 후보(근거 있을 때만). 결정은 심사자.

    risk_score(0~1)가 ERP 소유 임계 미만 = LOW 밴드 → 오탐 후보. 문자열 risk_level은 폴백.
    임계는 외부 응답(risk_thresholds)을 신뢰하지 않음(조작 시 고위험 건의 오탐 둔갑 차단).
    """
    score = out.get("risk_score")
    if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
        return min(max(float(score), 0.0), 1.0) < _FP_MEDIUM_THRESHOLD   # 0~1 클램프
    rl = str(out.get("risk_level") or out.get("risk") or "").strip().lower()
    return rl in _FP_RISK_LOW


def _dismiss_draft(out: dict) -> str:
    """A3 — 오탐(DISMISS) 사유 초안 생성. DISMISS는 사유 필수라 1-클릭 처리 가속."""
    head = "AI 분석: 삭감위험 낮음"
    score = out.get("risk_score")
    if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
        head += f"(score {min(max(float(score), 0.0), 1.0):.2f})"
    parts = [head + " — 오탐 후보"]
    rec = (out.get("recommendation") or "").strip()
    if rec:
        parts.append(rec)
    for c in (out.get("evidence_citations") or [])[:1]:
        if isinstance(c, dict):
            cite = (c.get("title") or c.get("notice_title") or "").strip()
            eff_date = (c.get("effective_from") or c.get("effective_date")
                        or c.get("date") or "").strip()
            if cite:
                parts.append(f"근거: {cite}{f'({eff_date})' if eff_date else ''}")
        elif c:
            parts.append(f"근거: {c}")
    parts.append("(검토자 확인 필요)")
    return " · ".join(parts)[:300]


@router.post("/pre-review/cases/{case_id}/ai-review")
async def case_ai_review(case_id: int,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """케이스 종합 AI 검토 (P1) — 미해소 적발을 weve로 일괄 진단(위험도·근거·권고).

    휴먼인더루프(결정=심사자)·비식별(filter_gate)·판단보류(근거 없으면 보류). 상병(진단)
    수신 시 컨텍스트 투입(의학적 타당성 보조). 결정 자동 변경 없음 — 참고 정보만.
    """
    import asyncio

    from erp.claims.models import PreReviewCase, PreReviewFinding
    from erp.config import get_settings
    from erp.integrations.weve_rag import WeveRagClient
    from erp.regulatory.filter_gate import scan as _scan_pii
    _require(user, "read")
    await _assert_case_dept(session, user, case_id)
    s = get_settings()
    # 사용자별 rate-limit — 케이스 루프로 전 환자·사번 테이블 반복 로드+적발별 스캔(DoS) 방어
    _review_limiter.check(
        f"aireview:{user.user_id}",
        max_calls=int(getattr(s, "weve_diagnose_rate_max", 20) or 20),
        window_s=float(getattr(s, "weve_diagnose_rate_window", 60.0) or 60.0))
    case = await session.get(PreReviewCase, case_id)
    if case is None:
        raise ValidationError("존재하지 않는 케이스입니다")
    if not s.weve_ai_api_key:
        return {"available": False, "note": "AI 평가 미연결(weve 키 미설정)", "items": []}
    dx = await _worklist().case_diagnoses(session, chart_no=case.chart_no,
                                          care_month=case.care_month)
    dx_ctx = ", ".join(dx)
    findings = (await session.scalars(select(PreReviewFinding).where(
        PreReviewFinding.case_id == case_id, PreReviewFinding.resolved.is_(False),
        PreReviewFinding.code.isnot(None)))).all()
    rag = s.weve_ai_api_base.rstrip("/")
    if rag.endswith("/api/notice"):
        rag = rag[: -len("/api/notice")]
    client = WeveRagClient(rag, s.weve_ai_api_key)

    # 표준코드 마스터 보강 — 약가코드 적발에 성분코드·ATC·전문일반을 컨텍스트로 투입(결정적
    # 데이터로 의학적 타당성 진단 보조·#313 최소필요정보 정합: 약품 특성이지 환자 PII 아님).
    from erp.regulatory.models import DrugStdCode
    codes = {f.code for f in findings if f.code}
    std_by_code: dict[str, str] = {}
    if codes:
        for pc, ic, atc, rx in (await session.execute(
                select(DrugStdCode.product_code, DrugStdCode.ingredient_code,
                       DrugStdCode.atc, DrugStdCode.rx_otc)
                .where(DrugStdCode.product_code.in_(codes),
                       DrugStdCode.canceled_on.is_(None)))).all():
            parts = [p for p in (f"성분코드 {ic}" if ic else None,
                                 f"ATC {atc}" if atc else None, rx) if p]
            if parts and pc not in std_by_code:
                std_by_code[pc] = "·".join(parts)

    # 비식별 게이트용 실데이터 사전 대조 — 요청당 1회 로드(적발별 재로드 방지). 숫자전용
    # 차트번호는 정규식만으론 미포착이라 known_ 주입 필수(외부 LLM 유출 방지·#774 후속).
    from erp.regulatory.filter_gate import pii_reference_sets
    _charts, _emps = await pii_reference_sets(session)

    # weve diagnose ≈ 3s/건 — 적발별 동시 호출(동시성 4 제한)로 '...' 체감 지연 해소(순서 보존).
    sem = asyncio.Semaphore(4)

    async def _diagnose_finding(f) -> dict:
        ctx = f"상병 {dx_ctx}; {f.detail or ''}" if dx_ctx else (f.detail or "")
        if f.code in std_by_code:
            ctx = f"{ctx}; 의약품({std_by_code[f.code]})" if ctx else f"의약품({std_by_code[f.code]})"
        base = {"finding_id": f.id, "code": f.code, "rule_name": f.rule_name}
        if _scan_pii(ctx, known_chart_nos=_charts, known_employee_nos=_emps).blocked:
            return {**base, "available": True, "held": True, "note": "비식별 차단 — 생략"}
        async with sem:
            try:
                out = await asyncio.to_thread(
                    client.diagnose, code=f.code, context=ctx,
                    service_date=f.treated_on.isoformat() if f.treated_on else None)
            except Exception:  # noqa: BLE001 — 내부 오류 본문 비노출
                return {**base, "available": False, "note": "AI 일시 오류"}
        if not out.get("has_evidence", bool(out.get("evidence_citations"))):
            return {**base, "available": True, "held": True, "note": "근거 고시 없음 — 판단 보류"}
        # 외부 응답은 신뢰경계 밖 — UI 계약 필드만 투영(과노출·미래필드 누출 차단)
        proj = {k: out.get(k) for k in _AI_SAFE_KEYS if k in out}
        rec = proj.get("recommendation")     # 생성형 텍스트 outbound 재검(비식별 양방향)
        if isinstance(rec, str) and _scan_pii(
                rec, known_chart_nos=_charts, known_employee_nos=_emps).blocked:
            proj["recommendation"] = "(비식별 차단)"
        item = {**base, "available": True, "held": False, **proj}
        if _fp_candidate(out):   # A3 — 오탐 사전제안 + 사유 초안(휴먼인더루프)
            item["false_positive_candidate"] = True
            item["dismiss_reason_draft"] = _dismiss_draft(out)
        return item

    items = list(await asyncio.gather(*(_diagnose_finding(f) for f in findings)))
    await _audit.record_access(session, actor=user.user_id, action="AI_REVIEW",
                               entity="claims.case_ai_review", target_id=str(case_id),
                               route="/claims/pre-review/cases/ai-review")   # §5 거버넌스 — AI 호출 감사
    return {"available": True, "case_id": case_id, "diagnosis_codes": dx, "items": items}


class QueryBody(BaseModel):
    question: str
    finding_id: int | None = None
    line_code: str | None = None
    assigned_to: int | None = None     # 의료진 employee_id


class ReplyBody(BaseModel):
    body: str
    reply_type: str                    # EVIDENCE | REVISE


class QueryResolveBody(BaseModel):
    status: str                        # ACCEPTED | REVISED
    note: str | None = None
    confirmed_by_employee: int | None = None    # 확인 책임 의료진(의학판단 적발 종결 시 필수)


@router.post("/pre-review/cases/{case_id}/queries", status_code=201)
async def create_query(case_id: int, body: QueryBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """의료진 질의 생성 (P2) — 적발/라인 단위 근거·수정 요청."""
    _require(user, "write")
    await _assert_case_dept(session, user, case_id)   # IDOR — 부서 경계
    q = await _worklist().create_query(
        session, actor=user.user_id, case_id=case_id, question=body.question,
        finding_id=body.finding_id, line_code=body.line_code,
        assigned_to=body.assigned_to)
    return {"id": q.id, "status": q.status}


@router.get("/pre-review/queries")
async def list_queries(case_id: int | None = None, assigned_to: int | None = None,
                       status: str | None = None,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """질의 목록 — 케이스 타임라인·의료진 내 질의함(assigned_to)·전역 관리 로그."""
    _require(user, "read")
    if case_id is not None:
        await _assert_case_dept(session, user, case_id)   # IDOR — 부서 경계
    items = await _worklist().list_queries(
        session, case_id=case_id, assigned_to=assigned_to, status=status,
        dept_codes=await _user_dept_codes(session, user))   # 전역 목록도 부서 경계(ALL만 전체)
    return {"count": len(items), "items": items}


@router.post("/pre-review/queries/{query_id}/replies", status_code=201)
async def reply_query(query_id: int, body: ReplyBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """의료진 회신 (P2) — 근거 제시(EVIDENCE) | 오더 수정(REVISE)."""
    _require(user, "write")
    await _assert_query_dept(session, user, query_id)   # IDOR — 부서 경계
    r = await _worklist().reply_query(
        session, actor=user.user_id, query_id=query_id,
        body=body.body, reply_type=body.reply_type)
    return {"id": r.id}


@router.patch("/pre-review/queries/{query_id}")
async def resolve_query(query_id: int, body: QueryResolveBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """질의 종결 (P2) — ACCEPTED(근거 인정→적발 DISMISS 연계) | REVISED(수정 완료)."""
    _require(user, "write")
    await _assert_query_dept(session, user, query_id)   # IDOR — 부서 경계(삭감 우회 차단)
    return await _worklist().resolve_query(
        session, actor=user.user_id, query_id=query_id,
        status=body.status, note=body.note,
        confirmed_by_employee=body.confirmed_by_employee)


class CaseUpdateBody(BaseModel):
    status: str | None = None
    assignee_id: int | None = None


@router.patch("/pre-review/cases/{case_id}")
async def worklist_case_update(case_id: int, body: CaseUpdateBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """상태 전이(규칙 검증)·담당 배정 — IN_REVIEW 진입 시 미배정이면 본인 배정."""
    _require(user, "write")
    await _assert_case_dept(session, user, case_id)   # IDOR — 부서 경계
    c = await _worklist().update_case(session, actor=user.user_id, case_id=case_id,
                                      status=body.status, assignee_id=body.assignee_id)
    return {"id": c.id, "status": c.status, "assignee_id": c.assignee_id}


class FindingDecisionBody(BaseModel):
    decision: str                       # ACCEPT | DISMISS
    note: str | None = None             # DISMISS는 사유 필수(캘리브레이션 데이터)


@router.patch("/pre-review/findings/{finding_id}/decision")
async def worklist_finding_decision(finding_id: int, body: FindingDecisionBody,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    await _assert_finding_dept(session, user, finding_id)   # IDOR — 부서 경계(삭감 우회 차단)
    f = await _worklist().decide_finding(session, actor=user.user_id,
                                         finding_id=finding_id,
                                         decision=body.decision, note=body.note)
    return {"id": f.id, "decision": f.decision}


class CommentBody(BaseModel):
    body: str


@router.post("/pre-review/cases/{case_id}/comments", status_code=201)
async def worklist_comment(case_id: int, body: CommentBody,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    await _assert_case_dept(session, user, case_id)   # IDOR — 부서 경계
    m = await _worklist().add_comment(session, actor=user.user_id,
                                      case_id=case_id, body=body.body)
    return {"id": m.id}


@router.post("/pre-review/sync-from-lines")
async def worklist_sync_from_lines(care_month: str,
                                   user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """진료라인 미러(D-6) → 워크리스트 자동 적재 — 케이스 멱등·해소 마킹 적용."""
    _require(user, "write")
    if await _user_dept_codes(session, user) is not None:   # DEPT(부서장)는 월 전체 sync 불가
        from erp.errors import ForbiddenError
        raise ForbiddenError("월 전체 sync는 관리자 권한 — 부서 케이스는 자동 sync가 처리합니다")
    return await _worklist().sync_from_billing_lines(session, actor=user.user_id,
                                                     care_month=care_month)


@router.get("/pre-review/stats")
async def worklist_stats(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """룰별 적발·오탐률 + 상태 분포 — 통계 탭·룰 보정 근거 (PR-B)."""
    _require(user, "read")
    return await _worklist().stats(session, dept_codes=await _user_dept_codes(session, user))


@router.get("/pre-review/triage")
async def worklist_triage(limit: int = 50,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """삭감위험 triage(P2-a) — 확정 결정 인정률 기반 미결 케이스 우선순위(휴먼인더루프)."""
    _require(user, "read")
    return await _worklist().triage(session, limit=min(limit, 200),
                                    dept_codes=await _user_dept_codes(session, user))


@router.get("/pre-review/auto-actions")
async def worklist_auto_actions(user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """자동 처리 근거·로그 — 자동 이상없음 케이스·자동 오탐 적발(검토·오버라이드용). 부서 경계."""
    _require(user, "read")
    return await _worklist().auto_actions(session,
                                          dept_codes=await _user_dept_codes(session, user))


class BulkStatusBody(BaseModel):
    items: list[dict]                   # [{chart_no, care_month, status}]


@router.post("/pre-review/cases/bulk-status")
async def worklist_bulk_status(body: BulkStatusBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """시트 왕복 일괄 상태 반영 — 키 불일치·전이 위반은 스킵 리포트."""
    _require(user, "write")
    return await _worklist().bulk_status(session, actor=user.user_id, items=body.items,
                                         dept_codes=await _user_dept_codes(session, user))


@router.get("/pre-review/cases-export.xlsx")
async def worklist_export(status: str | None = None, severity: str | None = None,
                          chart_no: str | None = None, encounter_type: str | None = None,
                          treated_on: date | None = None, care_month: str | None = None,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """워크리스트 xlsx 반출 — 팀 회의·시트 왕복용 (반출 감사). 목록과 동일 필터 적용."""
    from fastapi.responses import StreamingResponse

    from erp.common.excel import build_xlsx
    _require(user, "read")
    items = await _worklist().list_cases(session, status=status, severity=severity,
                                         chart_no=chart_no, encounter_type=encounter_type,
                                         treated_on=treated_on, care_month=care_month,
                                         dept_codes=await _user_dept_codes(session, user))
    rows = [[c["chart_no"], c["care_month"], c["status"], c["max_severity"] or "",
             c["finding_count"], c["risk_amount"], c["claimed_amt"],
             c["assignee_id"] or ""] for c in items]
    buf = build_xlsx(sheet_name="심사 워크리스트",
                     headers=["차트번호", "진료월", "상태", "심각도", "적발수",
                              "위험액(원)", "청구액(원)", "담당"], rows=rows)
    await _audit.record_audit(session, actor=user.user_id, action="EXPORT",
                              entity="claims.pre_review_cases", target_id=str(len(rows)),
                              after={"count": len(rows)})
    return StreamingResponse(
        iter([buf]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="pre-review-worklist.xlsx"'})


# ── 룰 관리 (PR-B) — 심사팀 셀프서비스, 변경은 ADMIN ──────
@router.get("/pre-review/rules")
async def list_rules(user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    from erp.claims.models import PreReviewRule
    _require(user, "read")
    rows = (await session.scalars(
        select(PreReviewRule).order_by(PreReviewRule.id))).all()
    return {"items": [{"id": r.id, "code": r.code, "name": r.name,
                       "rule_type": r.rule_type, "params": r.params,
                       "severity": r.severity, "active": r.active,
                       "physician_gated": r.physician_gated,
                       "valid_from": r.valid_from.isoformat() if r.valid_from else None,
                       "valid_to": r.valid_to.isoformat() if r.valid_to else None}
                      for r in rows]}


class RuleUpdateBody(BaseModel):
    name: str | None = None
    params: dict | None = None
    severity: str | None = None         # WARN | BLOCK
    active: bool | None = None
    physician_gated: bool | None = None  # 의학판단 필요(단독오탐 금지)
    valid_from: date | None = None       # 고시 효력일(G7) — 시점 유효룰
    valid_to: date | None = None         # 폐기일


@router.put("/pre-review/rules/{rule_id}")
async def update_rule(rule_id: int, body: RuleUpdateBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """룰 조정(단가·상한·활성) — 코드 수정 없이 운영 보정. ADMIN 전용·감사."""
    from erp.claims.models import PreReviewRule
    require_scope(user, "claims.rules", "write")
    r = await session.get(PreReviewRule, rule_id)
    if r is None:
        raise ValidationError("존재하지 않는 룰입니다")
    if body.severity and body.severity not in ("WARN", "BLOCK"):
        raise ValidationError("severity는 WARN|BLOCK")
    before = {"severity": r.severity, "active": r.active, "params": r.params}
    if body.name is not None:
        r.name = body.name
    if body.params is not None:
        r.params = body.params
    if body.severity is not None:
        r.severity = body.severity
    if body.active is not None:
        r.active = body.active
    if body.physician_gated is not None:
        r.physician_gated = body.physician_gated
    if body.valid_from is not None:
        r.valid_from = body.valid_from
    if body.valid_to is not None:
        r.valid_to = body.valid_to
    await _audit.record_audit(session, actor=user.user_id, action="UPDATE",
                              entity="claims.pre_review_rules", target_id=str(rule_id),
                              before=before,
                              after={"severity": r.severity, "active": r.active,
                                     "physician_gated": r.physician_gated})
    return {"id": r.id, "active": r.active, "severity": r.severity}


# ── 학습 사례 (AI 학습 데이터) — 결정 적발 자동수확 + 수동등록 + 내보내기 ──────
_learning_svc = None


def _learning():
    global _learning_svc
    if _learning_svc is None:
        from erp.claims.learning_service import LearningCaseService
        _learning_svc = LearningCaseService()
    return _learning_svc


@router.post("/pre-review/learning-cases/harvest")
async def harvest_learning_cases(user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """결정된 적발(인정/오탐) → 학습 사례 자동 수확(멱등). 의료진 확인분은 즉시 승인."""
    _require(user, "write")
    return await _learning().harvest(session, actor=user.user_id)


@router.get("/pre-review/learning-cases")
async def list_learning_cases(label: str | None = None, quality: str | None = None,
                              source: str | None = None, eligible_only: bool = False,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """학습 사례 목록 — 라벨/품질/소스/적격 필터 + 요약(적격·삭감·오탐 수)."""
    _require(user, "read")
    codes = await _user_dept_codes(session, user)   # IDOR/PII — 학습 사례 부서 경계
    return await _learning().list_cases(session, label=label, quality=quality, source=source,
                                  eligible_only=eligible_only, dept_codes=codes)


class LearningCreateBody(BaseModel):
    label: str                          # REDUCTION | FALSE_POSITIVE
    rationale: str | None = None
    source_finding_id: int | None = None
    code: str | None = None
    rule_code: str | None = None
    tags: str | None = None


@router.post("/pre-review/learning-cases", status_code=201)
async def create_learning_case(body: LearningCreateBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """수동 학습 사례 등록 — 적발(source_finding_id) 또는 자유 입력."""
    _require(user, "write")
    if body.source_finding_id is not None:   # IDOR — 타부서 적발을 학습셋에 취입 차단
        await _assert_finding_dept(session, user, body.source_finding_id)
    lc = await _learning().create(
        session, actor=user.user_id, label=body.label, rationale=body.rationale,
        source_finding_id=body.source_finding_id, code=body.code,
        rule_code=body.rule_code, tags=body.tags)
    return {"id": lc.id}


class LearningUpdateBody(BaseModel):
    label: str | None = None
    rationale: str | None = None
    quality: str | None = None          # DRAFT | APPROVED | REJECTED
    tags: str | None = None
    active: bool | None = None


@router.patch("/pre-review/learning-cases/{case_id}")
async def update_learning_case(case_id: int, body: LearningUpdateBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """학습 사례 수정 — 라벨·근거·품질(승인)·태그·활성."""
    _require(user, "write")
    await _assert_learning_dept(session, user, case_id)   # IDOR — 타부서 학습 사례 개변 차단
    lc = await _learning().update(
        session, actor=user.user_id, case_id=case_id, label=body.label,
        rationale=body.rationale, quality=body.quality, tags=body.tags, active=body.active)
    return {"id": lc.id, "label": lc.label, "quality": lc.quality}


@router.get("/pre-review/learning-cases/export")
async def export_learning_cases(user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """학습 적격 사례 JSONL 내보내기(외부 모델 학습용) — 반출 감사."""
    import json

    from fastapi.responses import StreamingResponse
    _require(user, "read")
    codes = await _user_dept_codes(session, user)   # IDOR/PII — 반출도 부서 경계
    records = await _learning().export(session, dept_codes=codes)
    await _audit.record_audit(session, actor=user.user_id, action="EXPORT",
                              entity="claims.pre_review_learning_cases",
                              target_id=str(len(records)), after={"count": len(records)})
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    return StreamingResponse(
        iter([body]), media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=prereview-learning.jsonl"})


# ── 변동대가(보험삭감 추정, K-IFRS 1115) ──────────────────────────
@router.get("/variable-consideration/preview")
async def variable_consideration_preview(yyyymm: str,
                                          user: CurrentUser = Depends(get_current_user),
                                          session: AsyncSession = Depends(get_session)):
    """미결정 청구 예상 삭감(변동대가) 미리보기 — 진료과 삭감률×청구액."""
    require_scope(user, "finance.periods", "read")
    from erp.claims.variable_consideration_service import VariableConsiderationService
    return await VariableConsiderationService().preview(session, yyyymm=yyyymm)


@router.post("/variable-consideration/post")
async def variable_consideration_post(yyyymm: str,
                                      user: CurrentUser = Depends(get_current_user),
                                      session: AsyncSession = Depends(get_session)):
    """변동대가 결산분개 전기(멱등) — 예상 삭감만큼 수익 차감/환불부채."""
    require_scope(user, "finance.periods", "write")
    from erp.claims.variable_consideration_service import VariableConsiderationService
    res = await VariableConsiderationService().post(session, yyyymm=yyyymm, actor=user.user_id)
    if res.get("posted"):
        await _audit.record_audit(session, actor=user.user_id, action="VAR_CONSIDERATION_POST",
                                  entity="finance.periods", target_id=str(res.get("voucher_id")),
                                  after={"yyyymm": yyyymm, "delta": res["delta"]})
        await session.commit()
    return res


# ── 사전심사 룰 적재(P0-1·G2·G1/G4·G6) — rule_loader 위임(시더와 SoT 공유) ──
# Row 검증·멱등 upsert는 erp.claims.rule_loader. EP는 scope·audit·commit만 담당.
from erp.claims.rule_loader import (  # noqa: E402  (라우터 하단 적재 섹션 지역 배치)
    BundleRuleRow,
    DurIngredientRow,
    DurRuleRow,
    DxRuleRow,
    SurchargeRateRow,
)


@router.post("/pre-review/dur/ingredient-map")
async def load_dur_ingredient_map(rows: list[DurIngredientRow],
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """보험코드→주성분 매핑 bulk upsert(DUR 선결 데이터)."""
    require_scope(user, "claims.rules", "write")   # 탐지 기준 변경 → ADMIN 전용
    from erp.claims import rule_loader
    n = await rule_loader.load_ingredient_map(session, rows)
    await _audit.record_audit(session, actor=user.user_id, action="LOAD",
                              entity="claims.drug_ingredient_map", target_id=str(n))
    await session.commit()
    return {"upserted": n}


@router.post("/pre-review/dur/rules")
async def load_dur_rules(rows: list[DurRuleRow], replace_source: str | None = None,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """DUR 룰 적재(공개데이터). replace_source 지정 시 해당 출처 전체 교체(고시 갱신)."""
    require_scope(user, "claims.rules", "write")   # 탐지 기준 변경 → ADMIN 전용
    from erp.claims import rule_loader
    n = await rule_loader.load_dur_rules(session, rows, replace_source=replace_source)
    await _audit.record_audit(session, actor=user.user_id, action="LOAD",
                              entity="claims.dur_rules", target_id=str(n))
    await session.commit()
    return {"loaded": n, "replaced_source": replace_source}


@router.get("/pre-review/dur/rules")
async def list_dur_rules(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """DUR 룰 현황."""
    _require(user, "read")
    rows = (await session.scalars(select(DurRule).order_by(DurRule.id))).all()
    return {"items": [{"id": r.id, "dur_type": r.dur_type, "ingredient_code": r.ingredient_code,
                       "ingredient_code_b": r.ingredient_code_b, "min_age": r.min_age,
                       "max_age": r.max_age, "reason": r.reason, "notice_no": r.notice_no,
                       "severity": r.severity, "source": r.source, "active": r.active}
                      for r in rows]}


@router.post("/pre-review/dx-rules")
async def load_dx_rules(rows: list[DxRuleRow],
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """진단-수가 연계 룰 bulk upsert(코드 기준)."""
    require_scope(user, "claims.rules", "write")   # 탐지 기준 변경 → ADMIN 전용
    from erp.claims import rule_loader
    n = await rule_loader.load_dx_rules(session, rows)
    await _audit.record_audit(session, actor=user.user_id, action="LOAD",
                              entity="claims.dx_procedure_rules", target_id=str(n))
    await session.commit()
    return {"upserted": n}


@router.get("/pre-review/dx-rules")
async def list_dx_rules(user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """진단-수가 연계 룰 현황."""
    _require(user, "read")
    rows = (await session.scalars(select(DxProcedureRule).order_by(DxProcedureRule.id))).all()
    return {"items": [{"id": r.id, "code": r.code, "require_dx_prefixes": r.require_dx_prefixes,
                       "name": r.name, "reason": r.reason, "severity": r.severity,
                       "active": r.active} for r in rows]}


@router.post("/pre-review/bundling-rules")
async def load_bundling_rules(rows: list[BundleRuleRow],
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """묶음청구 룰 적재(parent 패키지 + child 세부/재료). 자연키 멱등 upsert."""
    require_scope(user, "claims.rules", "write")   # 탐지 기준 변경 → ADMIN 전용
    from erp.claims import rule_loader
    n = await rule_loader.load_bundling_rules(session, rows)
    await _audit.record_audit(session, actor=user.user_id, action="LOAD",
                              entity="claims.bundling_rules", target_id=str(n))
    await session.commit()
    return {"upserted": n}


@router.get("/pre-review/bundling-rules")
async def list_bundling_rules(user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """묶음청구 룰 현황."""
    _require(user, "read")
    rows = (await session.scalars(select(BundlingRule).order_by(BundlingRule.id))).all()
    return {"items": [{"id": r.id, "parent_code": r.parent_code, "child_codes": r.child_codes,
                       "rule_type": r.rule_type, "name": r.name, "reason": r.reason,
                       "severity": r.severity, "active": r.active} for r in rows]}


@router.post("/pre-review/surcharge-rates")
async def load_surcharge_rates(rows: list[SurchargeRateRow],
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """종별가산율 bulk upsert(grade 기준). BASE_CATALOG 행위료에 ERP가 적용."""
    require_scope(user, "claims.rules", "write")   # 탐지 기준 변경 → ADMIN 전용
    from erp.claims import rule_loader
    n = await rule_loader.load_surcharge_rates(session, rows)
    await _audit.record_audit(session, actor=user.user_id, action="LOAD",
                              entity="regulatory.institution_surcharge_rates", target_id=str(n))
    await session.commit()
    return {"upserted": n}


@router.get("/pre-review/surcharge-rates")
async def list_surcharge_rates(user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """종별가산율 현황."""
    _require(user, "read")
    from erp.regulatory.models import InstitutionSurchargeRate
    rows = (await session.scalars(select(InstitutionSurchargeRate).order_by(
        InstitutionSurchargeRate.grade))).all()
    return {"items": [{"grade": r.grade, "rate": float(r.rate), "applies_to": r.applies_to,
                       "active": r.active} for r in rows]}


# ── 청구 산정(계산식) 미리보기 + EDI 매핑(격리 시드) ─────────────────
class CalcLineBody(BaseModel):
    code: str
    code_type: str = "TARIFF"          # TARIFF|DRUG|MATERIAL|NONCOVERED
    count: float = 1.0
    coverage: str = "NHIS"
    encounter_type: str = "OUTPATIENT"
    is_meal: bool = False
    name: str | None = None


class CalcPreviewBody(BaseModel):
    treated_on: date
    grade: str | None = None           # 종별(가산율 키)·미지정 시 프로필
    lines: list[CalcLineBody]
    with_edi: bool = False             # 격리 EDI 시드 매핑 동봉 여부
    provider_code: str | None = None   # 요양기호(EDI 시드 헤더·미확보 시 None)


@router.post("/calc/preview")
async def calc_preview(body: CalcPreviewBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """명세서 산정 미리보기 — 진료일(as-of) 단가·종별가산·묶음·본인부담 분리(회계 계산식).

    소급 재계산 안정: treated_on 시점 FeeMaster·가산율로 계산(과거 명세 재산정 동일결과).
    with_edi=True 시 **격리 시드** EDI 매핑 동봉(실 심평원 포맷 아님·제출 미연결).
    """
    _require(user, "read")
    from erp.claims.claim_calc import ClaimCalcEngine, ClaimLine
    # 종별 미지정 시 엔진이 진료일 시점 InstitutionGradeSetting(billing 권위 소스)에서 자체 해소.
    calc = await ClaimCalcEngine().calc_statement(
        session, treated_on=body.treated_on, grade=body.grade,
        lines=[ClaimLine(code=ln.code, code_type=ln.code_type, count=ln.count,
                         coverage=ln.coverage, encounter_type=ln.encounter_type,
                         is_meal=ln.is_meal, name=ln.name) for ln in body.lines])
    out = {"calc": calc}
    if body.with_edi:
        from erp.claims.edi_mapping import EDI_PRODUCTION_READY, map_to_edi
        # as_of=진료일 → 소급 재계산 시 당시 EDI 포맷 버전 자동 선택
        out["edi_seed"] = map_to_edi(calc, as_of=body.treated_on,
                                     provider_code=body.provider_code)
        out["edi_production_ready"] = EDI_PRODUCTION_READY
    return out


@router.get("/calc/from-chart")
async def calc_from_chart(chart_no: str, care_month: str | None = None,
                          encounter_id: str | None = None, with_edi: bool = False,
                          provider_code: str | None = None,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """실 진료내역(`patient.BillingLine`) 기반 청구 산정 — 라인별 진료일 as-of·종별가산·묶음.

    청구 라인 상세는 BillingLine(HIS 미러)에 영속. care_month/encounter_id로 명세 범위 좁힘.
    with_edi=True 시 격리 시드 EDI 매핑 동봉(제출 미연결)."""
    _require(user, "read")
    # 형식 검증은 스코프와 무관하게 먼저 — ALL 스코프는 부서 가드를 건너뛰어 잘못된 월이 엔진 파싱에서
    # ValueError 500이 됐다(2026-10-09 시나리오 2회차 S-B)
    if care_month is not None:
        try:
            date(int(care_month[:4]), int(care_month[5:7]), 1)
            if len(care_month) != 7 or care_month[4] != "-":
                raise ValueError(care_month)
        except ValueError as exc:
            raise ValidationError("진료월(care_month)은 YYYY-MM 형식이어야 합니다") from exc
    await _assert_chart_dept(session, user, chart_no=chart_no, care_month=care_month,
                             encounter_id=encounter_id)
    from erp.claims.claim_calc import ClaimCalcEngine
    calc = await ClaimCalcEngine().calc_for_chart(
        session, chart_no=chart_no, care_month=care_month, encounter_id=encounter_id)
    out = {"calc": calc}
    if with_edi and not calc.get("empty"):
        from erp.claims.edi_mapping import EDI_PRODUCTION_READY, map_to_edi
        out["edi_seed"] = map_to_edi(calc, provider_code=provider_code)
        out["edi_production_ready"] = EDI_PRODUCTION_READY
    return out
