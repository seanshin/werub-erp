"""자금결재 기안문서 — 자금 관련 품의·공람의 ERP 내부 결재(그룹웨어 전자결재와 별개·마이그 0213).

기획 general-eapproval-drafts.plan.md §10 적대검토 반영:
- R-2 템플릿 선택만: 상신 시 결재선 템플릿(ORG/PERSONAL) 선택 → engine.build_from_steps로 인스턴스.
- H-1 STAFF 결재: 결재 게이트는 라우터 `common.drafts approve`(전 직원) + 엔진 _eligible로 지정자만.
- H-3 회수: engine.cancel_instance(인스턴스 종결·행잠금·어느 단계 미결재 시만).
- M-1 내용 봉인: 상신 시 sha256(title|body|첨부해시)를 BUILD 원장 note에 기록.
- M-3 원자성: PUT·submit·cancel은 draft 행 with_for_update + 상태 재검증.
- M-5 알림: 상신·다음 단계 진행 시 지정 결재자(USER/DEPT_HEAD), 완료 시 기안자+공람자 알림.
회계·전표 없음(품의≠집행).
"""
import hashlib
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.approval_engine import ApprovalEngine
from erp.common.approval_line_service import ApprovalLineService
from erp.common.models import (
    ApprovalInstance, ApprovalStep, Attachment, Department, DraftViewer, GeneralDraft,
    Notification,
)
from erp.errors import ForbiddenError, ValidationError
from erp.common.kst import kst_iso

_audit = AuditService()
_DOC = "GENERAL_DRAFT"
_CATEGORIES = {"GENERAL", "BUDGET", "POLICY", "COOPERATION"}


class DraftService:
    def __init__(self):
        self._engine = ApprovalEngine()
        self._lines = ApprovalLineService()

    async def _get(self, session: AsyncSession, draft_id: int,
                   *, lock: bool = False) -> GeneralDraft:
        q = select(GeneralDraft).where(GeneralDraft.id == draft_id)
        if lock:
            q = q.with_for_update()
        d = await session.scalar(q)
        if d is None:
            raise ValidationError("존재하지 않는 기안문서입니다", {"draft_id": draft_id})
        return d

    async def create(self, session: AsyncSession, *, user_id: int, dept_id: int | None,
                     title: str, body: str | None, category: str = "GENERAL",
                     ref_amount: float | None = None) -> GeneralDraft:
        cat = (category or "GENERAL").upper()
        if cat not in _CATEGORIES:
            raise ValidationError("분류 오류", {"category": category})
        if not (title or "").strip():
            raise ValidationError("제목은 필수입니다")
        d = GeneralDraft(title=title[:200], body=body, category=cat, department_id=dept_id,
                         ref_amount=round(ref_amount) if ref_amount else None,
                         created_by=user_id)
        session.add(d)
        await session.flush()
        return d

    async def update(self, session: AsyncSession, *, draft_id: int, user_id: int,
                     title: str | None = None, body: str | None = None,
                     category: str | None = None,
                     ref_amount: float | None = None) -> GeneralDraft:
        """DRAFT 수정 — 기안자 본인·DRAFT만(행잠금·상신 레이스 방지·M-3)."""
        d = await self._get(session, draft_id, lock=True)
        if d.created_by != user_id:
            raise ForbiddenError("본인 기안문서만 수정할 수 있습니다")
        if d.status != "DRAFT":
            raise ValidationError("상신 후에는 수정할 수 없습니다", {"status": d.status})
        if title is not None:
            d.title = title[:200]
        if body is not None:
            d.body = body
        if category is not None:
            if category.upper() not in _CATEGORIES:
                raise ValidationError("분류 오류", {"category": category})
            d.category = category.upper()
        if ref_amount is not None:
            d.ref_amount = round(ref_amount)
        return d

    async def _next_doc_no(self, session: AsyncSession, year: int) -> str:
        from erp.common.doc_no import lock_doc_no
        await lock_doc_no(session, f"기안-{year}")   # 동시 상신 채번 경합 직렬화(v1.204)
        n = int(await session.scalar(
            select(func.count()).select_from(GeneralDraft).where(
                GeneralDraft.doc_no.like(f"기안-{year}-%"))) or 0) + 1
        return f"기안-{year}-{n:04d}"

    async def _content_hash(self, session: AsyncSession, d: GeneralDraft) -> str:
        """제목·본문·첨부(sha256 정렬)로 내용 봉인 해시(M-1) — 원장 note에 기록."""
        atts = (await session.scalars(
            select(Attachment.sha256).where(Attachment.entity_type == "draft",
                                            Attachment.entity_id == d.id)
            .order_by(Attachment.sha256))).all()
        payload = f"{d.title}\x1f{d.body or ''}\x1f{'|'.join(atts)}"
        return hashlib.sha256(payload.encode()).hexdigest()

    async def submit(self, session: AsyncSession, *, draft_id: int, user_id: int,
                     template_id: int, viewer_ids: list[int] | None = None,
                     on: date | None = None) -> GeneralDraft:
        """상신 — 결재선 템플릿 선택(R-2)·본문 봉인·doc_no 채번·공람자 지정."""
        d = await self._get(session, draft_id, lock=True)
        if d.created_by != user_id:
            raise ForbiddenError("본인 기안문서만 상신할 수 있습니다")
        if d.status != "DRAFT":
            raise ValidationError("상신 가능한 상태가 아닙니다", {"status": d.status})
        tpl = await self._lines.get_template(session, template_id)
        if not tpl.active:
            raise ValidationError("비활성 결재선입니다")
        if tpl.scope == "PERSONAL" and tpl.owner_user_id != user_id:
            raise ForbiddenError("본인 개인 결재선만 사용할 수 있습니다")
        dts = tpl.doc_types or ["ANY"]
        if "ANY" not in dts and _DOC not in dts:
            raise ValidationError("이 문서유형에 사용할 수 없는 결재선입니다",
                                  {"doc_types": dts})
        # SoD — 기안자가 결재 단계에 포함되면 자기결재. 엔진 act()가 작성자 결재를 막지만, 지정
        # 단계에 본인이 있으면 그 단계가 영원히 미결(막힘)이므로 상신 시 차단(가시적 오류).
        for s in tpl.steps:
            if (s.get("type") == "USER") and str(s.get("value")) == str(user_id):
                raise ValidationError("기안자 본인이 결재선에 포함될 수 없습니다")
        content = await self._content_hash(session, d)
        inst = await self._engine.build_from_steps(
            session, doc_type=_DOC, doc_id=d.id, steps=list(tpl.steps), created_by=user_id,
            department_id=d.department_id, amount=float(d.ref_amount or 0),
            note=f"기안 상신 tpl#{template_id} sha256:{content}")
        d.status = "PENDING"
        if d.doc_no is None:
            d.doc_no = await self._next_doc_no(session, (on or date.today()).year)
        # 공람자 등록(기안자 제외·중복 제거)
        for vid in {v for v in (viewer_ids or []) if v and v != user_id}:
            session.add(DraftViewer(draft_id=d.id, user_id=vid))
        await session.flush()
        await self._notify_current_approver(session, inst, d)
        return d

    async def _notify_current_approver(self, session: AsyncSession, inst: ApprovalInstance,
                                       d: GeneralDraft) -> None:
        """현재 단계 지정 결재자 알림(USER/DEPT_HEAD만·ROLE은 워크리스트 pull)."""
        step = await session.scalar(select(ApprovalStep).where(
            ApprovalStep.instance_id == inst.id, ApprovalStep.step_no == inst.current_step))
        if step is None:
            return
        uid = None
        if step.approver_type == "USER":
            uid = int(step.approver_value) if (step.approver_value or "").isdigit() else None
        elif step.approver_type == "DEPT_HEAD" and inst.department_id is not None:
            dept = await session.get(Department, inst.department_id)
            uid = dept.head_user_id if dept else None
        if uid:
            session.add(Notification(
                user_id=uid, type="APPROVAL",
                title=f"결재 요청 — {d.doc_no or ''} {d.title}"[:200],
                body=f"{d.category} 기안 결재 대기", href="/approvals"))

    async def approve(self, session: AsyncSession, *, draft_id: int, actor: int,
                      note: str = "", override: bool = False) -> GeneralDraft:
        """결재 승인 — 엔진 act(지정자 적격·SoD)·완주 시 APPROVED+공람/기안자 알림·다음 단계 알림."""
        d = await self._get(session, draft_id)
        if d.status != "PENDING":
            raise ValidationError("결재 가능한 상태가 아닙니다", {"status": d.status})
        inst = await self._engine.act(session, doc_type=_DOC, doc_id=draft_id, actor=actor,
                                      approve=True, note=note, override=override)
        if inst.status == "APPROVED":
            d.status, d.decided_at = "APPROVED", datetime.now(timezone.utc)
            await self._notify_completion(session, d)
        else:
            await self._notify_current_approver(session, inst, d)
        return d

    async def reject(self, session: AsyncSession, *, draft_id: int, actor: int,
                     note: str = "") -> GeneralDraft:
        d = await self._get(session, draft_id)
        if d.status != "PENDING":
            raise ValidationError("반려 가능한 상태가 아닙니다", {"status": d.status})
        await self._engine.act(session, doc_type=_DOC, doc_id=draft_id, actor=actor,
                               approve=False, note=note)
        d.status, d.decided_at = "REJECTED", datetime.now(timezone.utc)
        session.add(Notification(user_id=d.created_by, type="APPROVAL",
                                 title=f"기안 반려 — {d.doc_no or ''} {d.title}"[:200],
                                 body=(note or "반려됨"), href="/approvals"))
        return d

    async def _notify_completion(self, session: AsyncSession, d: GeneralDraft) -> None:
        """결재 완료 — 기안자 + 공람자 알림(공람은 열람 대기)."""
        now = datetime.now(timezone.utc)
        recipients = {d.created_by}
        viewers = (await session.scalars(
            select(DraftViewer).where(DraftViewer.draft_id == d.id))).all()
        for v in viewers:
            recipients.add(v.user_id)
            v.notified_at = now
        for uid in recipients:
            session.add(Notification(
                user_id=uid, type="APPROVAL",
                title=f"기안 결재완료 — {d.doc_no or ''} {d.title}"[:200],
                body=f"{d.category} 기안이 최종 승인되었습니다", href="/approvals"))

    async def cancel(self, session: AsyncSession, *, draft_id: int, user_id: int) -> GeneralDraft:
        """회수 — 기안자 본인. DRAFT는 즉시, PENDING은 어느 단계도 미결재 시만(H-3·인스턴스 종결)."""
        d = await self._get(session, draft_id, lock=True)
        if d.created_by != user_id:
            raise ForbiddenError("본인 기안문서만 회수할 수 있습니다")
        if d.status not in ("DRAFT", "PENDING"):
            raise ValidationError("회수 가능한 상태가 아닙니다", {"status": d.status})
        if d.status == "PENDING":
            ok = await self._engine.cancel_instance(session, doc_type=_DOC, doc_id=draft_id,
                                                    actor=user_id)
            if not ok:
                raise ValidationError("이미 결재가 진행되어 회수할 수 없습니다")
        d.status = "CANCELED"
        return d

    async def mark_read(self, session: AsyncSession, *, draft_id: int, user_id: int) -> dict:
        """공람 열람 확인 — 본인 공람 건만."""
        v = await session.scalar(select(DraftViewer).where(
            DraftViewer.draft_id == draft_id, DraftViewer.user_id == user_id))
        if v is None:
            raise ForbiddenError("공람 대상이 아닙니다")
        if v.read_at is None:
            v.read_at = datetime.now(timezone.utc)
        return {"draft_id": draft_id, "read_at": kst_iso(v.read_at)}

    # ── 조회(행 스코프) ─────────────────────────────────────
    async def _is_involved(self, session: AsyncSession, d: GeneralDraft, user_id: int) -> bool:
        """본인 관여 — 기안자·공람자·(지정)결재자(USER 단계). ROLE/DEPT_HEAD는 관여 판정에서 제외."""
        if d.created_by == user_id:
            return True
        if await session.scalar(select(DraftViewer.id).where(
                DraftViewer.draft_id == d.id, DraftViewer.user_id == user_id)):
            return True
        return bool(await session.scalar(
            select(ApprovalStep.id).join(ApprovalInstance,
                                         ApprovalInstance.id == ApprovalStep.instance_id)
            .where(ApprovalInstance.doc_type == _DOC, ApprovalInstance.doc_id == d.id,
                   ApprovalStep.approver_type == "USER",
                   ApprovalStep.approver_value == str(user_id)).limit(1)))

    async def get_detail(self, session: AsyncSession, *, draft_id: int, user: object) -> dict:
        from erp.common.policy import ScopeKind, require_scope
        scope = require_scope(user, "common.drafts", "read")
        d = await self._get(session, draft_id)
        if scope.kind == ScopeKind.SELF and not await self._is_involved(session, d, user.user_id):
            raise ForbiddenError("접근 권한이 없는 기안문서입니다")
        if scope.kind == ScopeKind.DEPT and d.department_id != user.dept_id \
                and not await self._is_involved(session, d, user.user_id):
            raise ForbiddenError("본인 부서 기안문서만 접근할 수 있습니다")
        line = await self._engine.get(session, doc_type=_DOC, doc_id=d.id)
        viewers = (await session.scalars(
            select(DraftViewer).where(DraftViewer.draft_id == d.id))).all()
        return {"id": d.id, "doc_no": d.doc_no, "title": d.title, "body": d.body,
                "category": d.category, "department_id": d.department_id,
                "ref_amount": float(d.ref_amount) if d.ref_amount is not None else None,
                "status": d.status, "created_by": d.created_by, "decided_at": d.decided_at,
                "approval": line,
                "viewers": [{"user_id": v.user_id, "read_at": v.read_at} for v in viewers]}

    async def list_drafts(self, session: AsyncSession, *, user: object,
                          status: str | None = None, page: int = 1, size: int = 50) -> dict:
        from erp.common.policy import ScopeKind, require_scope
        scope = require_scope(user, "common.drafts", "read")
        size = min(size, 200)
        q = select(GeneralDraft)
        if status:
            q = q.where(GeneralDraft.status == status)
        if scope.kind == ScopeKind.SELF:
            # 본인 기안 + 본인 공람 문서
            vids = select(DraftViewer.draft_id).where(DraftViewer.user_id == user.user_id)
            q = q.where((GeneralDraft.created_by == user.user_id)
                        | GeneralDraft.id.in_(vids))
        elif scope.kind == ScopeKind.DEPT:
            q = q.where(GeneralDraft.department_id == user.dept_id)
        rows = (await session.scalars(
            q.order_by(GeneralDraft.id.desc()).offset((page - 1) * size).limit(size))).all()
        return {"items": [{"id": d.id, "doc_no": d.doc_no, "title": d.title,
                           "category": d.category, "status": d.status,
                           "created_by": d.created_by, "decided_at": d.decided_at}
                          for d in rows], "page": page, "size": size}


# ── 기안 첨부 가드 (M-2 상신 후 동결·관여자 경계) — import 시 등록 ─────────────
async def _guard_row_draft(session, user, scope, entity_id):
    """첨부 접근 — SELF/DEPT scope면 관여자(기안자·공람자·USER결재자)/본부서만."""
    from erp.common.policy import ScopeKind
    d = await session.get(GeneralDraft, entity_id)
    if d is None:
        raise ForbiddenError()
    svc = DraftService()
    if scope.kind == ScopeKind.SELF:
        if not await svc._is_involved(session, d, user.user_id):
            raise ForbiddenError("관여하지 않은 기안문서의 첨부입니다")
    elif scope.kind == ScopeKind.DEPT:
        if d.department_id != user.dept_id \
                and not await svc._is_involved(session, d, user.user_id):
            raise ForbiddenError("본인 부서 기안문서의 첨부만 접근할 수 있습니다")


async def _exists_draft(session, entity_id) -> bool:
    """업로드 시 실재 확인 + 상신 후 첨부 동결(M-1 내용봉인 무력화 방지)."""
    d = await session.get(GeneralDraft, entity_id)
    if d is None:
        return False
    if d.status != "DRAFT":
        raise ValidationError("상신 후에는 첨부를 추가할 수 없습니다", {"status": d.status})
    return True


async def _guard_delete_draft(session, entity_id) -> None:
    """상신 후 첨부 삭제 차단(증빙 사후 제거 방지·M-2)."""
    d = await session.get(GeneralDraft, entity_id)
    if d is not None and d.status != "DRAFT":
        raise ValidationError("상신 후에는 첨부를 삭제할 수 없습니다", {"status": d.status})


def _register_draft_guards() -> None:
    from erp.common.attachment_router import (
        register_delete_guard, register_existence_check, register_row_guard,
    )
    register_row_guard("draft", _guard_row_draft)
    register_existence_check("draft", _exists_draft)
    register_delete_guard("draft", _guard_delete_draft)


_register_draft_guards()
