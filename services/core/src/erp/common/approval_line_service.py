"""결재선 요청·관리 (기안문서 선행 기반·마이그 0212).

사용자 결정(2026-07-21): R-1 ADMIN 겸임(별도 역할 없음)·R-2 템플릿 선택만·R-5 기존 ApprovalRule 공존.

- 결재선 템플릿: ORG(공용·요청 승인으로만 프로비저닝)/PERSONAL(개인·자유 CRUD).
- 결재선 요청: 표준(ORG) 신설/변경/폐기 요청 → 관리자(ADMIN) 검토 → 승인 시 템플릿 프로비저닝.
- steps 검증은 engine.validate_steps 공용. 결정 이력은 감사로그(FR-6·불변원장은 인스턴스 전용이라 미사용).
"""
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.approval_engine import validate_steps
from erp.common.models import ApprovalLineRequest, ApprovalLineTemplate
from erp.errors import ForbiddenError, ValidationError

_audit = AuditService()
_ENT_TPL = "common.approval_line_templates"
_ENT_REQ = "common.approval_line_requests"


def _norm_doc_types(doc_types: list | None) -> list:
    if not doc_types:
        return ["ANY"]
    return sorted({str(d).upper() for d in doc_types})


class ApprovalLineService:
    # ── 템플릿 조회 ─────────────────────────────────────────
    async def list_templates(self, session: AsyncSession, *, user_id: int,
                             doc_type: str | None = None) -> list[dict]:
        """선택 가능한 결재선 — 활성 ORG 공용 + 본인 PERSONAL. doc_type 지정 시 해당(또는 ANY)만."""
        q = select(ApprovalLineTemplate).where(
            ApprovalLineTemplate.active.is_(True),
            (ApprovalLineTemplate.scope == "ORG")
            | (ApprovalLineTemplate.owner_user_id == user_id))
        rows = (await session.scalars(q.order_by(
            ApprovalLineTemplate.scope, ApprovalLineTemplate.name))).all()
        items = []
        for t in rows:
            dts = t.doc_types or ["ANY"]
            if doc_type and "ANY" not in dts and doc_type.upper() not in dts:
                continue
            items.append(self._tpl_dict(t))
        return items

    def _tpl_dict(self, t: ApprovalLineTemplate) -> dict:
        return {"id": t.id, "name": t.name, "scope": t.scope,
                "owner_user_id": t.owner_user_id, "department_id": t.department_id,
                "doc_types": t.doc_types or ["ANY"], "steps": t.steps, "active": t.active,
                "source_request_id": t.source_request_id}

    async def get_template(self, session: AsyncSession, template_id: int,
                           ) -> ApprovalLineTemplate:
        t = await session.get(ApprovalLineTemplate, template_id)
        if t is None:
            raise ValidationError("존재하지 않는 결재선입니다", {"template_id": template_id})
        return t

    # ── 개인 결재선 CRUD (본인 전용) ─────────────────────────
    async def create_personal(self, session: AsyncSession, *, user_id: int, name: str,
                              steps: list[dict], doc_types: list | None = None,
                              ) -> ApprovalLineTemplate:
        t = ApprovalLineTemplate(name=name[:120], scope="PERSONAL", owner_user_id=user_id,
                                 doc_types=_norm_doc_types(doc_types),
                                 steps=validate_steps(steps), created_by=user_id)
        session.add(t)
        await session.flush()
        return t

    async def update_personal(self, session: AsyncSession, *, template_id: int, user_id: int,
                              name: str | None = None, steps: list[dict] | None = None,
                              doc_types: list | None = None) -> ApprovalLineTemplate:
        t = await self.get_template(session, template_id)
        if t.scope != "PERSONAL" or t.owner_user_id != user_id:
            raise ForbiddenError("본인 개인 결재선만 수정할 수 있습니다")
        if name is not None:
            t.name = name[:120]
        if steps is not None:
            t.steps = validate_steps(steps)
        if doc_types is not None:
            t.doc_types = _norm_doc_types(doc_types)
        t.updated_at = datetime.now(timezone.utc)
        return t

    async def delete_personal(self, session: AsyncSession, *, template_id: int,
                              user_id: int) -> None:
        t = await self.get_template(session, template_id)
        if t.scope != "PERSONAL" or t.owner_user_id != user_id:
            raise ForbiddenError("본인 개인 결재선만 삭제할 수 있습니다")
        await session.delete(t)
        await session.flush()

    # ── 결재선 요청 (전 직원 접수 → ADMIN 검토) ──────────────
    async def _next_req_no(self, session: AsyncSession, year: int) -> str:
        from erp.common.doc_no import lock_doc_no
        await lock_doc_no(session, f"결재선요청-{year}")   # 채번 경합 직렬화(v1.204)
        n = int(await session.scalar(
            select(func.count()).select_from(ApprovalLineRequest).where(
                ApprovalLineRequest.req_no.like(f"결재선요청-{year}-%"))) or 0) + 1
        return f"결재선요청-{year}-{n:04d}"

    async def submit_request(self, session: AsyncSession, *, requested_by: int,
                             request_type: str, reason: str,
                             proposed_name: str | None = None,
                             proposed_steps: list[dict] | None = None,
                             doc_types: list | None = None,
                             target_template_id: int | None = None,
                             department_id: int | None = None,
                             on: date | None = None) -> ApprovalLineRequest:
        """표준(ORG) 결재선 신설/변경/폐기 요청. NEW/MODIFY는 제안 steps 필수·검증."""
        rtype = (request_type or "").upper()
        if rtype not in ("NEW", "MODIFY", "RETIRE"):
            raise ValidationError("요청 유형 오류", {"request_type": request_type})
        if not (reason or "").strip():
            raise ValidationError("요청 사유는 필수입니다")
        steps = None
        if rtype in ("NEW", "MODIFY"):
            if not proposed_steps:
                raise ValidationError("제안 결재 단계가 필요합니다")
            steps = validate_steps(proposed_steps)
        if rtype in ("MODIFY", "RETIRE"):
            t = await self.get_template(session, target_template_id) if target_template_id \
                else None
            if t is None or t.scope != "ORG":
                raise ValidationError("변경/폐기 대상 공용 결재선이 필요합니다",
                                      {"target_template_id": target_template_id})
        if rtype == "NEW" and not (proposed_name or "").strip():
            raise ValidationError("신설 결재선 이름이 필요합니다")
        year = (on or date.today()).year
        req = ApprovalLineRequest(
            req_no=await self._next_req_no(session, year), request_type=rtype,
            target_template_id=target_template_id, proposed_name=proposed_name,
            proposed_steps=steps or [], doc_types=_norm_doc_types(doc_types),
            reason=reason[:400], department_id=department_id, requested_by=requested_by)
        session.add(req)
        await session.flush()
        await _audit.record_audit(session, actor=requested_by, action="CREATE",
                                  entity=_ENT_REQ, target_id=str(req.id),
                                  after={"req_no": req.req_no, "type": rtype})
        return req

    async def cancel_request(self, session: AsyncSession, *, request_id: int,
                             user_id: int) -> ApprovalLineRequest:
        req = await self._get_request(session, request_id)
        if req.requested_by != user_id:
            raise ForbiddenError("본인 요청만 회수할 수 있습니다")
        if req.status != "REQUESTED":
            raise ValidationError("처리된 요청은 회수할 수 없습니다", {"status": req.status})
        req.status = "CANCELED"
        return req

    async def _get_request(self, session: AsyncSession, request_id: int,
                           *, lock: bool = False) -> ApprovalLineRequest:
        req = await session.get(ApprovalLineRequest, request_id,
                                with_for_update=lock)
        if req is None:
            raise ValidationError("존재하지 않는 요청입니다", {"request_id": request_id})
        return req

    async def review_request(self, session: AsyncSession, *, request_id: int, approve: bool,
                             reviewer: int, note: str | None = None) -> dict:
        """관리자(ADMIN) 검토 — 승인 시 ORG 템플릿 프로비저닝(NEW 생성·MODIFY 교체·RETIRE 비활성).

        감사 M3: 행잠금(동시 이중 프로비저닝 방지)·직무분리(요청자 본인 검토 불가)."""
        req = await self._get_request(session, request_id, lock=True)
        if req.status != "REQUESTED":
            raise ValidationError("이미 처리된 요청입니다", {"status": req.status})
        if req.requested_by == reviewer:
            raise ForbiddenError("직무분리 — 요청자 본인은 검토할 수 없습니다")
        req.reviewed_by, req.reviewed_at = reviewer, datetime.now(timezone.utc)
        req.review_note = (note or "")[:400] or None
        provisioned = None
        if not approve:
            req.status = "REJECTED"
        else:
            req.status = "APPROVED"
            if req.request_type == "NEW":
                t = ApprovalLineTemplate(
                    name=req.proposed_name or f"결재선 {req.req_no}", scope="ORG",
                    department_id=req.department_id, doc_types=req.doc_types,
                    steps=req.proposed_steps, source_request_id=req.id, created_by=reviewer)
                session.add(t)
                await session.flush()
                provisioned = t.id
            elif req.request_type == "MODIFY":
                t = await self.get_template(session, req.target_template_id)
                t.steps = req.proposed_steps
                t.doc_types = req.doc_types
                t.updated_at = datetime.now(timezone.utc)
                provisioned = t.id
            elif req.request_type == "RETIRE":
                t = await self.get_template(session, req.target_template_id)
                t.active = False
                t.updated_at = datetime.now(timezone.utc)
                provisioned = t.id
        await _audit.record_audit(
            session, actor=reviewer, action="APPROVE" if approve else "REJECT",
            entity=_ENT_REQ, target_id=str(req.id),
            after={"status": req.status, "template_id": provisioned})
        return {"request_id": req.id, "status": req.status, "template_id": provisioned}

    async def list_requests(self, session: AsyncSession, *, status: str | None = None,
                            requested_by: int | None = None) -> list[dict]:
        q = select(ApprovalLineRequest)
        if status:
            q = q.where(ApprovalLineRequest.status == status)
        if requested_by is not None:
            q = q.where(ApprovalLineRequest.requested_by == requested_by)
        rows = (await session.scalars(q.order_by(ApprovalLineRequest.id.desc()).limit(500))).all()
        return [{"id": r.id, "req_no": r.req_no, "request_type": r.request_type,
                 "target_template_id": r.target_template_id, "proposed_name": r.proposed_name,
                 "proposed_steps": r.proposed_steps, "doc_types": r.doc_types,
                 "reason": r.reason, "status": r.status, "requested_by": r.requested_by,
                 "reviewed_by": r.reviewed_by, "review_note": r.review_note} for r in rows]
