"""N단계 결재선 라우터 — 결재선 룰 설정(관리자) + 결재 워크리스트·상태 조회.

내부 ERP 다단계 승인(PO·계약). HIS 연동 전자결재(integrations.eapproval, 휴가)와는 별개.
실제 결재 행위(approve/reject)는 도메인 엔드포인트(PO·계약)에서 엔진을 호출 — 도메인 객체 전이 동반.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.approval_engine import ApprovalEngine
from erp.common.models import ApprovalRule
from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.errors import ValidationError

router = APIRouter(prefix="/approvals", tags=["approvals"])
_engine = ApprovalEngine()
_DOC_TYPES = {"PO", "OUTSOURCING_CONTRACT", "MEDICAL_CONTRACT", "ITAM_DISPOSAL", "VOUCHER",
              "EXPENSE", "GENERAL_DRAFT"}
_STEP_TYPES = {"ROLE", "DEPT_HEAD", "USER"}


class ApprovalStepSpec(BaseModel):
    type: str               # ROLE|DEPT_HEAD|USER
    value: str | None = None


class ApprovalRuleBody(BaseModel):
    doc_type: str
    category: str | None = None
    min_amount: float = 0
    max_amount: float | None = None
    steps: list[ApprovalStepSpec]
    active: bool = True


@router.get("/rules")
async def list_rules(user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    require_scope(user, "common.approvals", "read")
    rows = (await session.scalars(select(ApprovalRule).order_by(
        ApprovalRule.doc_type, ApprovalRule.min_amount))).all()
    return {"items": [{"id": r.id, "doc_type": r.doc_type, "category": r.category,
                       "min_amount": float(r.min_amount),
                       "max_amount": float(r.max_amount) if r.max_amount is not None else None,
                       "steps": r.steps, "active": r.active} for r in rows]}


@router.post("/rules", status_code=201)
async def create_rule(body: ApprovalRuleBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    require_scope(user, "common.approval_rules", "write")
    if body.doc_type not in _DOC_TYPES:
        raise ValidationError(f"문서 유형 오류: {body.doc_type}")
    if not body.steps:
        raise ValidationError("결재 단계가 최소 1개 필요합니다")
    for s in body.steps:
        if s.type.upper() not in _STEP_TYPES:
            raise ValidationError(f"결재 단계 유형 오류: {s.type}")
        if s.type.upper() in ("ROLE", "USER") and not s.value:
            raise ValidationError(f"{s.type} 단계는 value가 필요합니다")
    r = ApprovalRule(doc_type=body.doc_type, category=body.category,
                     min_amount=body.min_amount, max_amount=body.max_amount,
                     steps=[{"type": s.type.upper(), "value": s.value} for s in body.steps],
                     active=body.active, created_by=user.user_id)
    session.add(r)
    await session.flush()
    return {"id": r.id}


@router.patch("/rules/{rule_id}")
async def toggle_rule(rule_id: int, active: bool,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    require_scope(user, "common.approval_rules", "write")
    r = await session.get(ApprovalRule, rule_id)
    if r is None:
        raise ValidationError("존재하지 않는 룰입니다")
    r.active = active
    return {"id": r.id, "active": r.active}


# ── 결재선 요청·관리 (기안문서 선행 기반·0212) ─────────────────────────────
# ⚠ 반드시 아래 `/{doc_type}/{doc_id}` catch-all보다 먼저 등록(경로 우선순위).
_line_svc = None


def _lines():
    global _line_svc
    if _line_svc is None:
        from erp.common.approval_line_service import ApprovalLineService
        _line_svc = ApprovalLineService()
    return _line_svc


@router.get("/lines")
async def list_approval_lines(doc_type: str | None = None,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """선택 가능한 결재선 — 활성 공용(ORG) + 본인 개인(PERSONAL). 상신 화면 결재선 선택용."""
    require_scope(user, "common.approval_lines", "read")
    return {"items": await _lines().list_templates(session, user_id=user.user_id,
                                                   doc_type=doc_type)}


@router.get("/lines/directory")
async def approver_directory(user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """결재자 선택 디렉터리 — 활성 사용자 id·이름·부서(개인 결재선/요청 단계 구성용).

    최소필요(v1.203): role·이메일 로컬파트 폴백 제거 — 전 직원 노출 EP라 조직 역할 분포·
    로그인 계정 힌트를 주지 않는다(itam employee_options와 동급 수준)."""
    require_scope(user, "common.approval_lines", "read")
    from erp.audit.service import AuditService
    from erp.common.models import User
    from erp.hr.models import Employee
    rows = (await session.execute(
        select(User.id, User.department_id, Employee.name)
        .join(Employee, Employee.id == User.employee_id, isouter=True)
        .where(User.is_active.is_(True)).order_by(User.id).limit(2000))).all()
    # 접속기록(v1.204) — 전 직원 로스터 조회는 hr.employees 목록과 동급으로 기록
    await AuditService().record_access(session, actor=user.user_id, action="VIEW",
                                       entity="common.approver_directory",
                                       route="/approvals/lines/directory")
    return {"items": [{"id": r.id, "name": r.name or f"사용자{r.id}",
                       "department_id": r.department_id} for r in rows]}


class PersonalLineBody(BaseModel):
    name: str
    steps: list[ApprovalStepSpec]
    doc_types: list[str] | None = None


@router.post("/lines/personal", status_code=201)
async def create_personal_line(body: PersonalLineBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """개인 결재선 생성(본인 전용) — 자주 쓰는 결재자 순서 저장."""
    require_scope(user, "common.approval_lines", "write")
    t = await _lines().create_personal(
        session, user_id=user.user_id, name=body.name, doc_types=body.doc_types,
        steps=[s.model_dump() for s in body.steps])
    await session.commit()
    return {"id": t.id, "name": t.name}


@router.patch("/lines/personal/{template_id}")
async def update_personal_line(template_id: int, body: PersonalLineBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    require_scope(user, "common.approval_lines", "write")
    t = await _lines().update_personal(
        session, template_id=template_id, user_id=user.user_id, name=body.name,
        doc_types=body.doc_types, steps=[s.model_dump() for s in body.steps])
    await session.commit()
    return {"id": t.id, "name": t.name}


@router.delete("/lines/personal/{template_id}")
async def delete_personal_line(template_id: int,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    require_scope(user, "common.approval_lines", "write")
    await _lines().delete_personal(session, template_id=template_id, user_id=user.user_id)
    await session.commit()
    return {"deleted": template_id}


class LineRequestBody(BaseModel):
    request_type: str                      # NEW | MODIFY | RETIRE
    reason: str
    proposed_name: str | None = None
    proposed_steps: list[ApprovalStepSpec] | None = None
    doc_types: list[str] | None = None
    target_template_id: int | None = None


@router.post("/lines/requests", status_code=201)
async def submit_line_request(body: LineRequestBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """표준(ORG) 결재선 신설/변경/폐기 요청 — 전 직원 접수, ADMIN 검토 대기."""
    require_scope(user, "common.approval_lines", "write")
    req = await _lines().submit_request(
        session, requested_by=user.user_id, request_type=body.request_type,
        reason=body.reason, proposed_name=body.proposed_name,
        proposed_steps=[s.model_dump() for s in body.proposed_steps]
        if body.proposed_steps else None,
        doc_types=body.doc_types, target_template_id=body.target_template_id,
        department_id=user.dept_id)
    await session.commit()
    return {"id": req.id, "req_no": req.req_no, "status": req.status}


@router.get("/lines/requests")
async def list_line_requests(status: str | None = None, mine: bool = False,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """결재선 요청 목록 — mine=본인 요청. 전체 조회는 관리자(승인 대기함)."""
    if mine:
        require_scope(user, "common.approval_lines", "read")
        return {"items": await _lines().list_requests(session, status=status,
                                                       requested_by=user.user_id)}
    require_scope(user, "common.approval_lines", "admin")
    return {"items": await _lines().list_requests(session, status=status)}


@router.post("/lines/requests/{request_id}/cancel")
async def cancel_line_request(request_id: int,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    require_scope(user, "common.approval_lines", "write")
    req = await _lines().cancel_request(session, request_id=request_id, user_id=user.user_id)
    await session.commit()
    return {"id": req.id, "status": req.status}


class LineReviewBody(BaseModel):
    approve: bool
    note: str | None = None


@router.post("/lines/requests/{request_id}/review")
async def review_line_request(request_id: int, body: LineReviewBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """결재선 요청 검토(승인/반려) — ADMIN 겸임(R-1). 승인 시 ORG 템플릿 프로비저닝."""
    require_scope(user, "common.approval_lines", "admin")
    out = await _lines().review_request(session, request_id=request_id, approve=body.approve,
                                        reviewer=user.user_id, note=body.note)
    await session.commit()
    return out


# ── 자금결재 기안문서(0213·ERP 내부 결재) — 결재선(0212) 선택 소비 ─────────────
# ⚠ `/{doc_type}/{doc_id}` catch-all보다 먼저 등록.
_draft_svc = None


def _drafts():
    global _draft_svc
    if _draft_svc is None:
        from erp.common.draft_service import DraftService
        _draft_svc = DraftService()
    return _draft_svc


class DraftBody(BaseModel):
    title: str
    body: str | None = None
    category: str = "GENERAL"
    ref_amount: float | None = None


@router.post("/drafts", status_code=201)
async def create_draft(body: DraftBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """기안문서 작성(DRAFT) — 첨부는 attachments(entity_type=draft)."""
    require_scope(user, "common.drafts", "write")
    d = await _drafts().create(session, user_id=user.user_id, dept_id=user.dept_id,
                               title=body.title, body=body.body, category=body.category,
                               ref_amount=body.ref_amount)
    await session.commit()
    return {"id": d.id, "status": d.status}


@router.get("/drafts")
async def list_drafts(status: str | None = None, page: int = 1, size: int = 50,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    return await _drafts().list_drafts(session, user=user, status=status, page=page, size=size)


@router.get("/drafts/{draft_id}")
async def get_draft(draft_id: int,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    return await _drafts().get_detail(session, draft_id=draft_id, user=user)


@router.put("/drafts/{draft_id}")
async def update_draft(draft_id: int, body: DraftBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    require_scope(user, "common.drafts", "write")
    d = await _drafts().update(session, draft_id=draft_id, user_id=user.user_id,
                               title=body.title, body=body.body, category=body.category,
                               ref_amount=body.ref_amount)
    await session.commit()
    return {"id": d.id, "status": d.status}


class DraftSubmitBody(BaseModel):
    template_id: int                       # 결재선 템플릿(0212) 선택 — R-2
    viewer_ids: list[int] | None = None


@router.post("/drafts/{draft_id}/submit")
async def submit_draft(draft_id: int, body: DraftSubmitBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """상신 — 결재선 템플릿 선택·본문 봉인·doc_no 채번."""
    require_scope(user, "common.drafts", "write")
    d = await _drafts().submit(session, draft_id=draft_id, user_id=user.user_id,
                               template_id=body.template_id, viewer_ids=body.viewer_ids)
    await session.commit()
    return {"id": d.id, "doc_no": d.doc_no, "status": d.status}


class DraftActBody(BaseModel):
    note: str = ""
    override: bool = False


@router.post("/drafts/{draft_id}/approve")
async def approve_draft(draft_id: int, body: DraftActBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """결재 승인 — 게이트 common.drafts approve(전 직원)·지정 결재자 판정은 엔진(H-1)."""
    require_scope(user, "common.drafts", "approve")
    d = await _drafts().approve(session, draft_id=draft_id, actor=user.user_id,
                                note=body.note, override=body.override)
    await session.commit()
    return {"id": d.id, "status": d.status}


@router.post("/drafts/{draft_id}/reject")
async def reject_draft(draft_id: int, body: DraftActBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    require_scope(user, "common.drafts", "approve")
    d = await _drafts().reject(session, draft_id=draft_id, actor=user.user_id, note=body.note)
    await session.commit()
    return {"id": d.id, "status": d.status}


@router.post("/drafts/{draft_id}/cancel")
async def cancel_draft(draft_id: int,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    require_scope(user, "common.drafts", "write")
    d = await _drafts().cancel(session, draft_id=draft_id, user_id=user.user_id)
    await session.commit()
    return {"id": d.id, "status": d.status}


@router.post("/drafts/{draft_id}/read")
async def read_draft(draft_id: int,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """공람 열람 확인."""
    require_scope(user, "common.drafts", "read")
    out = await _drafts().mark_read(session, draft_id=draft_id, user_id=user.user_id)
    await session.commit()
    return out


@router.get("/ledger/verify")
async def verify_approval_ledger(user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """결재 불변 원장(해시체인) 무결성 검증 — 전 구간 재계산 대조(변조·삭제·순서교란 탐지).

    감사 성격이라 admin.audit read(DIRECTOR/ADMIN). 체인은 DB append-only(REVOKE·0203) —
    본 검증은 그 우회(superuser)까지 사후 탐지하는 상위 통제.
    """
    from erp.common.approval_ledger import ApprovalLedgerService
    require_scope(user, "admin.audit", "read")
    return await ApprovalLedgerService().verify(session)


@router.get("/ledger/sign-log")
async def approval_ledger_sign_log(limit: int = 20, offset: int = 0,
                                   user: CurrentUser = Depends(get_current_user)):
    """sign 블록체인 봉인 이력 + sign측 체인 검증(B안) — 자금결재 원장 앵커의 외부 시점봉인 조회.

    ERP 원장 verify(위 EP)와 독립인 **외부 증명**: 일일 검증 잡이 앵커 head를 sign 해시체인에
    제출(TSA 시점봉인)한 기록. admin.audit read(DIRECTOR/ADMIN). sign 미설정/불가 시
    available=False(빈 목록·비차단).

    limit/offset 배선(적대검토 C-M5): 고정 20건이면 21건째부터 조회 자체가 불가했고, 같은
    스트림에 잡음 20건을 append하면 정품 봉인 기록을 **화면 밖으로 밀어낼 수** 있었다.
    """
    require_scope(user, "admin.audit", "read")
    from erp.config import get_settings
    if not get_settings().sign_api_key:
        return {"available": False, "reason": "SIGN_API_KEY 미설정", "entries": [], "verify": None}
    def _kst_str(iso: str | None) -> str | None:
        """sign ISO 문자열(UTC Z)을 KST 벽시계 표기로 — 문자열이라 전역 인코더 비대상."""
        if not iso or not isinstance(iso, str):
            return iso if isinstance(iso, str) else None   # 비문자열은 버린다(파싱 예외 방지)
        from datetime import datetime as dt

        from erp.common.kst import as_kst_wallclock
        try:
            d = as_kst_wallclock(dt.fromisoformat(iso.replace("Z", "+00:00")))
            return d.isoformat(sep=" ", timespec="seconds")
        except (ValueError, TypeError):
            return iso
    try:
        from erp.integrations.sign_client import (
            FUND_APPROVAL_STREAM, list_audit_events, verify_audit_chain,
        )
        lim = max(1, min(int(limit), 100))
        off = max(0, int(offset))
        lst = await list_audit_events(stream=FUND_APPROVAL_STREAM, limit=lim, offset=off)
        chk = await verify_audit_chain(stream=FUND_APPROVAL_STREAM)
        # 외부(신뢰 없는) 응답 방어 파싱 — 한 필드가 예상 밖이어도 카드 전체가 죽지 않도록.
        raw = lst.get("data") if isinstance(lst, dict) else None
        entries = [{**e, "occurredAt": _kst_str(e.get("occurredAt"))}
                   for e in (raw or []) if isinstance(e, dict)]
        if not isinstance(chk, dict):
            chk = {"valid": None, "note": "sign 검증 응답 형식 이상"}
        chk["anchoredAt"] = _kst_str(chk.get("anchoredAt"))
        return {"available": True, "entries": entries, "verify": chk,
                "page": {"limit": lim, "offset": off, "returned": len(entries)}}
    except Exception as exc:  # noqa: BLE001 — sign 장애는 조회 실패로만 표시(500 방지)
        return {"available": False, "reason": str(exc)[:120], "entries": [], "verify": None}


@router.get("/{doc_type}/{doc_id}/ledger")
async def approval_ledger_entries(doc_type: str, doc_id: int,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """문서별 결재 원장 로그(불변 해시체인 항목) — 결재 이력의 변조 증거 노출."""
    from erp.audit.service import AuditService
    from erp.common.approval_ledger import ApprovalLedgerService
    require_scope(user, "common.approvals", "read")
    if doc_type not in _DOC_TYPES:
        raise ValidationError(f"문서 유형 오류: {doc_type}")
    # actor·note(자유텍스트) 노출이라 접속기록 남김(적대검토 LOW — 조회 감사 부재)
    await AuditService().record_access(
        session, actor=user.user_id, action="VIEW", entity="common.approval_ledger",
        target_id=f"{doc_type}:{doc_id}", route="/approvals/{doc_type}/{doc_id}/ledger")
    return {"doc_type": doc_type, "doc_id": doc_id,
            "items": await ApprovalLedgerService().entries_for(
                session, doc_type=doc_type, doc_id=doc_id)}


@router.get("/worklist")
async def my_worklist(user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """내가 결재해야 할 진행 단계 목록(작성자 본인 건 제외)."""
    require_scope(user, "common.approvals", "read")
    items = await _engine.pending_for(session, actor=user.user_id, role=user.role,
                                      dept_id=user.dept_id)
    return {"items": items}


@router.get("/{doc_type}/{doc_id}")
async def approval_status(doc_type: str, doc_id: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """문서의 결재 진행 상태(단계별 결정 이력)."""
    require_scope(user, "common.approvals", "read")
    if doc_type not in _DOC_TYPES:
        raise ValidationError(f"문서 유형 오류: {doc_type}")
    inst = await _engine.get(session, doc_type=doc_type, doc_id=doc_id)
    return inst or {"status": None}
