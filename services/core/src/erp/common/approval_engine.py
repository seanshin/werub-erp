"""N단계 결재선 엔진 (공통) — PO·위수탁계약·의료계약이 공유하는 다단계 승인.

순수 공통 엔진(도메인 import 없음, common.models·audit만 의존). 각 도메인은 submit 시 build()로
결재선을 생성하고 approve/reject 시 act()로 진행한다. 룰이 없으면 build()는 None을 반환 →
호출측은 기존 단일 승인(직무분리)으로 폴백(무중단 호환).

승인자 유형:
- ROLE: 행위자 role == approver_value (예: MANAGER·DIRECTOR)
- DEPT_HEAD: 행위자가 문서 귀속 부서(department_id)의 부서장(Department.head_user_id)
- USER: 행위자 user_id == int(approver_value)

직무분리: 행위자 ≠ 인스턴스 생성자(작성자). 단계 승인자는 위 유형을 충족해야 한다.
"""
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.models import (
    ApprovalInstance, ApprovalRule, ApprovalStep, Department, Notification, User,
)
from erp.errors import ValidationError

# 결재 문서유형 라벨(알림 본문·P1-3)
_DOC_LABELS = {"PO": "구매발주", "OUTSOURCING_CONTRACT": "위수탁계약",
               "MEDICAL_CONTRACT": "의료진계약", "ITAM_DISPOSAL": "IT자산폐기",
               "VOUCHER": "회계전표", "EXPENSE": "경비청구", "GENERAL_DRAFT": "기안문서"}

_audit = AuditService()
_VALID_TYPES = {"ROLE", "DEPT_HEAD", "USER"}
_EXEC_ROLE = "DIRECTOR"     # 최고경영자(원장) — 전권 결재 권한
_VALID_ROLES = {"DIRECTOR", "ADMIN", "MANAGER", "STAFF",
                "IT_MANAGER", "FINANCE_MANAGER", "LOGISTICS_MANAGER"}


def validate_steps(steps: list[dict], *, max_steps: int = 8) -> list[dict]:
    """결재 단계 검증·정규화 (결재선 템플릿·요청·ad-hoc 공용).

    규칙: 1~max_steps단계·유형 유효(ROLE/DEPT_HEAD/USER)·ROLE/USER는 value 필수·ROLE value는 유효 역할·
    USER value는 정수·동일 결재자(같은 type+value) 중복 불가. 정규화된 steps(type 대문자·value 문자열) 반환.
    """
    if not steps:
        raise ValidationError("결재 단계가 최소 1개 필요합니다")
    if len(steps) > max_steps:
        raise ValidationError(f"결재 단계는 최대 {max_steps}개입니다", {"count": len(steps)})
    out, seen = [], set()
    for s in steps:
        stype = (s.get("type") or "").upper()
        if stype not in _VALID_TYPES:
            raise ValidationError(f"결재 단계 유형 오류: {stype}")
        val = s.get("value")
        val = str(val) if val is not None else None
        if stype in ("ROLE", "USER") and not val:
            raise ValidationError(f"{stype} 단계는 value가 필요합니다")
        if stype == "ROLE" and val not in _VALID_ROLES:
            raise ValidationError(f"결재 역할 오류: {val}")
        if stype == "USER" and not val.isdigit():
            raise ValidationError("USER 단계 value는 사용자 id(정수)입니다", {"value": val})
        key = (stype, val)
        if key in seen:
            raise ValidationError("동일 결재자가 중복되었습니다", {"step": key})
        seen.add(key)
        out.append({"type": stype, "value": val})
    return out


class ApprovalEngine:
    async def find_rule(self, session: AsyncSession, *, doc_type: str, amount: float,
                        category: str | None = None) -> ApprovalRule | None:
        """문서유형·금액대·분류에 맞는 활성 룰 — category 일치 우선, 없으면 전체(null) 룰."""
        rows = (await session.scalars(select(ApprovalRule).where(
            ApprovalRule.doc_type == doc_type, ApprovalRule.active.is_(True)))).all()
        matches = [r for r in rows
                   if (r.category is None or r.category == category)
                   and amount >= float(r.min_amount)
                   and (r.max_amount is None or amount < float(r.max_amount))]
        if not matches:
            return None
        # 결정적 선택: category 명시 우선 → 좁은 밴드(높은 min_amount) → id (중첩 밴드도 일관)
        matches.sort(key=lambda r: (r.category is None, -float(r.min_amount), r.id))
        return matches[0]

    async def build(self, session: AsyncSession, *, doc_type: str, doc_id: int, amount: float,
                    created_by: int, department_id: int | None = None,
                    category: str | None = None) -> ApprovalInstance | None:
        """결재선 생성 — 매칭 룰의 steps로 인스턴스+단계 생성. 룰 없으면 None(레거시 폴백)."""
        # 진행 중 인스턴스를 **룰 조회보다 먼저** 본다 (2026-10-09 전체 흐름 검토 D·MED-1).
        # 종전엔 룰이 없으면 곧장 None을 돌려, 결재 도중 룰을 끄거나 금액밴드를 바꾸면 다음 승인
        # 호출이 레거시 단일 승인으로 폴백해 **남은 단계를 건너뛰고** 전기·확정됐다. 한 번 상신된
        # 결재선은 상신 시점의 steps 스냅샷으로 끝까지 진행해야 한다(룰 변경은 신규 상신부터).
        existing = await session.scalar(select(ApprovalInstance).where(
            ApprovalInstance.doc_type == doc_type, ApprovalInstance.doc_id == doc_id,
            ApprovalInstance.status == "PENDING"))
        if existing is not None:
            return existing       # 이미 진행 중(멱등)
        rule = await self.find_rule(session, doc_type=doc_type, amount=amount, category=category)
        if rule is None or not rule.steps:
            return None
        return await self._materialize(session, doc_type=doc_type, doc_id=doc_id,
                                       amount=amount, created_by=created_by,
                                       department_id=department_id, steps=list(rule.steps))

    async def build_from_steps(self, session: AsyncSession, *, doc_type: str, doc_id: int,
                               steps: list[dict], created_by: int,
                               department_id: int | None = None,
                               amount: float = 0, note: str | None = None) -> ApprovalInstance:
        """명시 steps로 결재선 생성 (결재선 템플릿 선택·ad-hoc 공용) — 룰 매칭 불요.

        결재선 템플릿(선택형 문서·기안문서 등)이 자신의 steps 스냅샷으로 인스턴스를 만든다. 이미 진행 중
        (PENDING) 인스턴스가 있으면 멱등 반환. steps는 validate_steps로 검증(호출측 미검증 대비).
        note: 불변원장 BUILD 기록 문구(기안문서는 내용 해시 봉인 — 적대검토 M-1).
        """
        existing = await session.scalar(select(ApprovalInstance).where(
            ApprovalInstance.doc_type == doc_type, ApprovalInstance.doc_id == doc_id,
            ApprovalInstance.status == "PENDING"))
        if existing is not None:
            return existing
        steps = validate_steps(steps)
        return await self._materialize(session, doc_type=doc_type, doc_id=doc_id,
                                       amount=amount, created_by=created_by,
                                       department_id=department_id, steps=steps, note=note)

    async def cancel_instance(self, session: AsyncSession, *, doc_type: str, doc_id: int,
                              actor: int) -> bool:
        """진행 중 결재선 회수 — 인스턴스 행잠금 후 어느 단계도 미결재일 때만 CANCELED(적대검토 H-3).

        좀비 워크리스트·취소 문서에 APPROVE가 원장에 기록되는 것 방지. 인스턴스 없으면 True(무해·멱등),
        이미 어느 단계 결재됨(진행 중)이면 False(회수 불가). 원장 CANCEL 기록.
        """
        inst = await session.scalar(select(ApprovalInstance).where(
            ApprovalInstance.doc_type == doc_type, ApprovalInstance.doc_id == doc_id,
            ApprovalInstance.status == "PENDING").with_for_update())
        if inst is None:
            return True
        acted = await session.scalar(select(ApprovalStep.id).where(
            ApprovalStep.instance_id == inst.id, ApprovalStep.status != "PENDING").limit(1))
        if acted is not None:
            return False
        now = datetime.now(timezone.utc)
        inst.status, inst.decided_at = "CANCELED", now
        await _audit.record_audit(session, actor=actor, action="CANCEL",
                                  entity="common.approval_instances", target_id=str(inst.id),
                                  after={"status": "CANCELED"})
        from erp.common.approval_ledger import ApprovalLedgerService
        await ApprovalLedgerService().append(
            session, instance_id=inst.id, doc_type=doc_type, doc_id=doc_id, step_no=None,
            action="CANCEL", actor=actor, acted_at=now, note="상신 회수")
        return True

    async def _materialize(self, session: AsyncSession, *, doc_type: str, doc_id: int,
                           amount: float, created_by: int, department_id: int | None,
                           steps: list[dict], note: str | None = None) -> ApprovalInstance:
        """인스턴스+단계 실체화 + 감사/불변원장 기록 (build·build_from_steps 공용)."""
        # 부서장 단계가 있으면 귀속 부서 필수 — 없으면 영원히 미해결(워크리스트 비노출) 방지.
        if any((s.get("type") or "").upper() == "DEPT_HEAD" for s in steps) \
                and department_id is None:
            raise ValidationError("부서장 결재 단계에는 문서의 귀속 부서가 필요합니다")
        inst = ApprovalInstance(doc_type=doc_type, doc_id=doc_id, amount=amount,
                                department_id=department_id, status="PENDING",
                                current_step=1, created_by=created_by)
        session.add(inst)
        await session.flush()
        for i, step in enumerate(steps, start=1):
            stype = (step.get("type") or "").upper()
            if stype not in _VALID_TYPES:
                raise ValidationError(f"결재 단계 유형 오류: {stype}")
            session.add(ApprovalStep(
                instance_id=inst.id, step_no=i, approver_type=stype,
                approver_value=(str(step["value"]) if step.get("value") is not None else None)))
        await session.flush()
        await _audit.record_audit(session, actor=created_by, action="CREATE",
                                  entity="common.approval_instances", target_id=str(inst.id),
                                  after={"doc_type": doc_type, "doc_id": doc_id,
                                         "steps": len(steps), "amount": amount})
        from erp.common.approval_ledger import ApprovalLedgerService
        await ApprovalLedgerService().append(
            session, instance_id=inst.id, doc_type=doc_type, doc_id=doc_id, step_no=None,
            action="BUILD", actor=created_by, acted_at=datetime.now(timezone.utc),
            note=note or f"결재선 {len(steps)}단계 상신")
        await self._notify_step(session, inst)   # 1단계 적격자에 상신 알림(P1-3)
        return inst

    async def _eligible(self, session: AsyncSession, step: ApprovalStep, actor: int,
                        department_id: int | None) -> bool:
        u = await session.get(User, actor)
        if u is None:
            return False
        if step.approver_type == "ROLE":
            return u.role == step.approver_value
        if step.approver_type == "USER":
            return str(actor) == (step.approver_value or "")
        if step.approver_type == "DEPT_HEAD":
            if department_id is None:
                return False
            dept = await session.get(Department, department_id)
            return dept is not None and dept.head_user_id == actor
        return False

    async def can_act_current_step(self, session: AsyncSession, *, doc_type: str, doc_id: int,
                                   actor: int, override: bool = False) -> bool:
        """행위자가 진행 중 결재선의 **현재 단계** 결재 적격자인가(또는 전권 DIRECTOR인가).

        도메인 승인 EP의 인가 보조 판정 — 판정만 하고 상태는 바꾸지 않는다(실제 검증·진행은 act()).
        진행 중(PENDING) 인스턴스가 없으면 False(레거시 단일 승인은 도메인 권한 경로만)."""
        inst = await session.scalar(select(ApprovalInstance).where(
            ApprovalInstance.doc_type == doc_type, ApprovalInstance.doc_id == doc_id,
            ApprovalInstance.status == "PENDING"))
        if inst is None or inst.created_by == actor:      # 직무분리 — 작성자 본인 불가
            return False
        step = await session.scalar(select(ApprovalStep).where(
            ApprovalStep.instance_id == inst.id, ApprovalStep.step_no == inst.current_step))
        if step is None or step.status != "PENDING":
            return False
        if override:
            u = await session.get(User, actor)
            return u is not None and u.role == _EXEC_ROLE
        return await self._eligible(session, step, actor, inst.department_id)

    async def _notify_step(self, session: AsyncSession, inst: ApprovalInstance) -> None:
        """현재 단계 적격 결재자에게 '내 차례' 알림(P1-3) — ROLE/USER/DEPT_HEAD 해소."""
        step = await session.scalar(select(ApprovalStep).where(
            ApprovalStep.instance_id == inst.id, ApprovalStep.step_no == inst.current_step))
        if step is None or step.status != "PENDING":
            return
        uids: list[int] = []
        if step.approver_type == "USER" and (step.approver_value or "").isdigit():
            uids = [int(step.approver_value)]
        elif step.approver_type == "ROLE":
            uids = list((await session.scalars(select(User.id).where(
                User.role == step.approver_value, User.is_active.is_(True)))).all())
        elif step.approver_type == "DEPT_HEAD" and inst.department_id is not None:
            dept = await session.get(Department, inst.department_id)
            if dept is not None and dept.head_user_id is not None:
                uids = [dept.head_user_id]
        label = _DOC_LABELS.get(inst.doc_type, inst.doc_type)
        body = f"{label} #{inst.doc_id} {inst.current_step}단계 결재 대기"
        for uid in uids:
            session.add(Notification(user_id=uid, type="APPROVAL_PENDING",
                                     title="결재 대기", body=body))

    async def act(self, session: AsyncSession, *, doc_type: str, doc_id: int, actor: int,
                  approve: bool, note: str = "", override: bool = False) -> ApprovalInstance:
        """현재 단계 승인/반려. 승인 시 다음 단계로 진행, 마지막이면 인스턴스 APPROVED.

        반려 시 인스턴스 REJECTED. 직무분리(행위자≠작성자)·단계 승인자 적격 검증.
        override=True(최고경영자 전권): DIRECTOR가 결재선을 건너뛰어 즉시 확정(잔여 단계 SKIPPED).
        직무분리는 전권에도 유지(작성자 본인 불가).
        """
        # 인스턴스 행잠금 — 동시 이중승인(더블클릭/재시도) 직렬화. 미잠금 시 두 트랜잭션이
        # 같은 PENDING 스냅샷을 읽어 마지막 단계를 둘 다 APPROVED 처리 → 예산 이중 encumber·
        # 자산 이중 처분/GL 등 후처리 이중 적용(전체검토 HIGH). 두 번째 호출은 커밋 후
        # status가 PENDING이 아니게 되어 아래 None 분기로 반려됨.
        inst = await session.scalar(select(ApprovalInstance).where(
            ApprovalInstance.doc_type == doc_type, ApprovalInstance.doc_id == doc_id,
            ApprovalInstance.status == "PENDING").with_for_update())
        if inst is None:
            raise ValidationError("진행 중인 결재가 없습니다")
        if inst.created_by == actor:
            raise ValidationError("직무분리 — 작성자 본인은 결재할 수 없습니다")
        step = await session.scalar(select(ApprovalStep).where(
            ApprovalStep.instance_id == inst.id, ApprovalStep.step_no == inst.current_step))
        if step is None or step.status != "PENDING":
            raise ValidationError("결재할 단계가 없습니다")
        if override:
            return await self._override(session, inst, step, actor, note)
        if not await self._eligible(session, step, actor, inst.department_id):
            raise ValidationError("이 단계의 결재 권한이 없습니다")
        now = datetime.now(timezone.utc)
        step.acted_by, step.acted_at, step.note = actor, now, (note or None)
        if not approve:
            step.status = "REJECTED"
            inst.status, inst.decided_at = "REJECTED", now
        else:
            step.status = "APPROVED"
            last = await session.scalar(select(ApprovalStep.step_no).where(
                ApprovalStep.instance_id == inst.id).order_by(ApprovalStep.step_no.desc()))
            if inst.current_step >= (last or inst.current_step):
                inst.status, inst.decided_at = "APPROVED", now
            else:
                inst.current_step += 1
                await self._notify_step(session, inst)   # 다음 단계 적격자에 "내 차례" 알림(P1-3)
        await _audit.record_audit(session, actor=actor,
                                  action="APPROVE" if approve else "REJECT",
                                  entity="common.approval_steps", target_id=str(step.id),
                                  after={"instance": inst.id, "step": step.step_no,
                                         "instance_status": inst.status})
        # 불변 원장(해시체인) — 승인/반려 기록
        from erp.common.approval_ledger import ApprovalLedgerService
        await ApprovalLedgerService().append(
            session, instance_id=inst.id, doc_type=inst.doc_type, doc_id=inst.doc_id,
            step_no=step.step_no, action="APPROVE" if approve else "REJECT",
            actor=actor, acted_at=now, note=note or None)
        return inst

    async def _override(self, session: AsyncSession, inst: ApprovalInstance, step: ApprovalStep,
                        actor: int, note: str) -> ApprovalInstance:
        """최고경영자 전권 결재 — 결재선 건너뛰어 즉시 확정. DIRECTOR 전용·잔여 단계 SKIPPED."""
        u = await session.get(User, actor)
        if u is None or u.role != _EXEC_ROLE:
            raise ValidationError("전권 결재 권한이 없습니다 (최고경영자 전용)")
        now = datetime.now(timezone.utc)
        step.acted_by, step.acted_at = actor, now
        step.status = "APPROVED"
        step.note = ("[전권] " + (note or "")).strip()
        rest = (await session.scalars(select(ApprovalStep).where(
            ApprovalStep.instance_id == inst.id, ApprovalStep.step_no > inst.current_step,
            ApprovalStep.status == "PENDING"))).all()
        for s in rest:
            s.status, s.note = "SKIPPED", "[전권 생략]"
        inst.status, inst.decided_at = "APPROVED", now
        await _audit.record_audit(session, actor=actor, action="OVERRIDE",
                                  entity="common.approval_instances", target_id=str(inst.id),
                                  after={"doc_type": inst.doc_type, "doc_id": inst.doc_id,
                                         "skipped": len(rest), "note": note})
        # 불변 원장(해시체인) — 전권 결재 기록(생략 단계 수 포함)
        from erp.common.approval_ledger import ApprovalLedgerService
        await ApprovalLedgerService().append(
            session, instance_id=inst.id, doc_type=inst.doc_type, doc_id=inst.doc_id,
            step_no=step.step_no, action="OVERRIDE", actor=actor, acted_at=now,
            note=f"[전권·{len(rest)}단계 생략] {note or ''}".strip())
        return inst

    async def get(self, session: AsyncSession, *, doc_type: str, doc_id: int) -> dict | None:
        """문서의 최신 결재 인스턴스+단계(표시용). 없으면 None."""
        inst = await session.scalar(select(ApprovalInstance).where(
            ApprovalInstance.doc_type == doc_type, ApprovalInstance.doc_id == doc_id)
            .order_by(ApprovalInstance.id.desc()))
        if inst is None:
            return None
        steps = (await session.scalars(select(ApprovalStep).where(
            ApprovalStep.instance_id == inst.id).order_by(ApprovalStep.step_no))).all()
        return {"id": inst.id, "doc_type": doc_type, "doc_id": doc_id,
                "status": inst.status, "current_step": inst.current_step,
                # 의료계약 금액=의료진 보수(민감) — 결재 화면엔 비노출(hr.payroll 권한자만 도메인에서 조회)
                "amount": None if doc_type == "MEDICAL_CONTRACT" else float(inst.amount),
                "steps": [{"step_no": s.step_no, "approver_type": s.approver_type,
                           "approver_value": s.approver_value, "status": s.status,
                           "acted_by": s.acted_by, "acted_at": s.acted_at, "note": s.note}
                          for s in steps]}

    async def pending_for(self, session: AsyncSession, *, actor: int, role: str,
                          dept_id: int | None) -> list[dict]:
        """행위자가 결재 가능한 PENDING 단계 워크리스트(작성자 본인 건 제외).

        단일 조인 쿼리(현재단계 step + 부서 outer join)로 적격 단계만 선별 — N+1 제거.
        """
        cur = and_(ApprovalStep.instance_id == ApprovalInstance.id,
                   ApprovalStep.step_no == ApprovalInstance.current_step,
                   ApprovalStep.status == "PENDING")
        rows = (await session.execute(
            select(ApprovalInstance, ApprovalStep)
            .join(ApprovalStep, cur)
            .outerjoin(Department, Department.id == ApprovalInstance.department_id)
            .where(ApprovalInstance.status == "PENDING",
                   or_(ApprovalInstance.created_by.is_(None),
                       ApprovalInstance.created_by != actor),
                   or_(and_(ApprovalStep.approver_type == "ROLE",
                            ApprovalStep.approver_value == role),
                       and_(ApprovalStep.approver_type == "USER",
                            ApprovalStep.approver_value == str(actor)),
                       and_(ApprovalStep.approver_type == "DEPT_HEAD",
                            Department.head_user_id == actor))))).all()
        return [{"instance_id": inst.id, "doc_type": inst.doc_type, "doc_id": inst.doc_id,
                 "amount": (None if inst.doc_type == "MEDICAL_CONTRACT" else float(inst.amount)),
                 "step_no": step.step_no, "approver_type": step.approver_type}
                for inst, step in rows]


async def require_scope_or_step_approver(session: AsyncSession, user, resource: str, action: str,
                                         *, doc_type: str, doc_id: int,
                                         override: bool = False):
    """도메인 권한 **또는** 진행 중 결재선의 현재 단계 적격자 인가 (승인·반려 EP 공용).

    2026-10-09 시나리오 S-E D1/S-D M2: 결재선 룰은 MANAGER·DIRECTOR 같은 ROLE 단계를 받고 엔진도
    그걸 전제(전권 override=DIRECTOR)로 설계됐는데, 도메인 승인 EP가 도메인 write 권한(전표=
    finance.vouchers, 발주·계약=scm.po)으로만 게이트해 그 단계는 **영원히 403**(결재 불가)·DIRECTOR
    전권도 403이었다. 도메인 권한이 없으면 현재 단계 적격 여부로 한 번 더 판정한다(작성자 본인·
    작업대 출입증은 불가). 결재선이 없는 문서(레거시 단일 승인)는 종전대로 도메인 권한만 인정.
    반환: 도메인 Scope(권한 경로) 또는 None(결재자 경로)."""
    from erp.common.policy import require_scope
    from erp.errors import ForbiddenError
    try:
        return require_scope(user, resource, action)
    except ForbiddenError:
        if user.role != "WORKSTATION" and await ApprovalEngine().can_act_current_step(
                session, doc_type=doc_type, doc_id=doc_id, actor=user.user_id,
                override=override):
            return None
        raise
