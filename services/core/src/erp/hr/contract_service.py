"""의료진 개별 계약 (P4-1) — 생애주기(DRAFT→승인→발효→해지/갱신)·직원당 ACTIVE 1건(신규 승계).

성과급 산정(P4-3)은 별도. 보수=민감 → 접근은 라우터에서 RBAC(hr.payroll).
상태·전이는 위수탁계약과 공통 커널(common.contract_lifecycle)로 일원화. 변경이력은 common.ContractAmendment(kind=MEDICAL).
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.approval_engine import ApprovalEngine
from erp.common.contract_lifecycle import assert_transition
from erp.common.models import ContractAmendment
from erp.errors import ValidationError
from erp.hr.models import Employee, MedicalStaffContract

_audit = AuditService()
_approval = ApprovalEngine()
_INCENTIVE_TYPES = {"NONE", "NET_COLLECTION", "DEPT_SHARE", "TARGET_EXCESS", "RVU"}
_KIND = "MEDICAL"
_DOC = "MEDICAL_CONTRACT"
_ENT = "hr.medical_contracts"


class MedicalContractService:
    async def create(self, session: AsyncSession, *, actor: int, employee_id: int,
                     valid_from: date, base_pay: float, duty_pay: float | None = None,
                     incentive_type: str = "NONE", incentive_rate: float | None = None,
                     incentive_params: dict | None = None, exclusive: bool = True,
                     require_approval: bool = False,
                     note: str | None = None) -> MedicalStaffContract:
        emp = await session.get(Employee, employee_id)
        if emp is None:
            raise ValidationError("존재하지 않는 직원입니다")
        if emp.staff_class != "MEDICAL":
            raise ValidationError("의료진(MEDICAL)만 개별 계약 대상입니다")
        if incentive_type not in _INCENTIVE_TYPES:
            raise ValidationError(f"성과급 유형 오류: {incentive_type}")
        if incentive_type != "NONE" and incentive_rate is None and incentive_type != "RVU":
            raise ValidationError("성과급 배분율(incentive_rate)이 필요합니다")
        if base_pay <= 0:
            raise ValidationError("기본급은 0보다 커야 합니다")
        # 결재선 룰이 매칭되면 승인 경로(DRAFT) 강제 — 결재선 우회 방지(보안검토).
        if not require_approval and await _approval.find_rule(
                session, doc_type=_DOC, amount=float(base_pay)):
            require_approval = True
        # 즉시 발효(현행 호환)면 기존 ACTIVE 승계, 결재 경로(DRAFT)면 발효 시점에 승계.
        if not require_approval:
            await self._supersede_active(session, employee_id, valid_from)
        c = MedicalStaffContract(
            employee_id=employee_id, valid_from=valid_from, effective_on=valid_from,
            base_pay=base_pay, duty_pay=duty_pay, incentive_type=incentive_type,
            incentive_rate=incentive_rate, incentive_params=incentive_params or {},
            exclusive=exclusive, note=note,
            status="DRAFT" if require_approval else "ACTIVE", created_by=actor)
        session.add(c)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE", entity=_ENT,
                                  target_id=str(c.id),
                                  after={"employee_id": employee_id, "base_pay": base_pay,
                                         "incentive_type": incentive_type, "status": c.status})
        return c

    def _transition(self, c: MedicalStaffContract, to: str) -> None:
        assert_transition(c.status, to)
        c.status = to

    async def _supersede_active(self, session: AsyncSession, employee_id: int,
                                from_on: date, *, exclude_id: int | None = None) -> None:
        """직원의 기존 ACTIVE/EXPIRING 계약을 신규 발효 전일자로 종료(TERMINATED)."""
        prev = await session.scalar(select(MedicalStaffContract).where(
            MedicalStaffContract.employee_id == employee_id,
            MedicalStaffContract.status.in_(("ACTIVE", "EXPIRING"))))
        if prev is None or (exclude_id is not None and prev.id == exclude_id):
            return
        if from_on <= prev.valid_from:
            raise ValidationError("신규 계약 시작일은 기존 계약 시작일 이후여야 합니다")
        prev.valid_to = from_on - timedelta(days=1)
        prev.status = "TERMINATED"
        prev.terminated_on = from_on - timedelta(days=1)

    async def submit_for_approval(self, session: AsyncSession, *, actor: int,
                                  contract_id: int) -> MedicalStaffContract:
        c = await self._get(session, contract_id)
        self._transition(c, "PENDING_APPROVAL")
        await _approval.build(session, doc_type=_DOC, doc_id=contract_id,
                              amount=float(c.base_pay), created_by=c.created_by or actor,
                              department_id=None)
        return c

    async def approve(self, session: AsyncSession, *, actor: int, contract_id: int,
                      override: bool = False) -> MedicalStaffContract:
        """승인 — 결재선 있으면 다단계(완주 시 APPROVED), 없으면 레거시 단일 승인(직무분리).

        override=True(최고경영자 전권): DIRECTOR가 결재선 건너뛰어 즉시 확정(직무분리 유지).
        """
        c = await self._get(session, contract_id)
        inst = await _approval.get(session, doc_type=_DOC, doc_id=contract_id)
        if (inst is None or inst.get("status") != "PENDING") and c.status in (
                "DRAFT", "PENDING_APPROVAL"):
            # 2026-10-09 전체 흐름 검토 E(scm 위수탁 계약과 동일 결함) — 상신(submit) 없이 DRAFT에서
            # 바로 approve하면 결재선이 생성되지 않아 룰이 매칭돼도 '레거시 단일 승인'으로 빠졌다
            # (의료진 보수 계약의 다단계 결재 우회). 승인 시점에도 룰로 결재선을 만들고 그 결재선을 탄다.
            built = await _approval.build(session, doc_type=_DOC, doc_id=contract_id,
                                          amount=float(c.base_pay),
                                          created_by=c.created_by or actor, department_id=None)
            if built is not None:
                if c.status == "DRAFT":
                    self._transition(c, "PENDING_APPROVAL")
                inst = {"status": "PENDING"}
        if inst is not None and inst.get("status") == "PENDING":
            out = await _approval.act(session, doc_type=_DOC, doc_id=contract_id, actor=actor,
                                      approve=True, override=override)
            if out.status != "APPROVED":
                return c                       # 다음 단계 대기
        elif c.created_by == actor:            # 레거시 직무분리
            raise ValidationError("직무분리 — 작성자 본인은 계약을 승인할 수 없습니다")
        self._transition(c, "APPROVED")
        c.approved_by, c.approved_at = actor, datetime.now(timezone.utc)
        await _audit.record_audit(session, actor=actor, action="APPROVE", entity=_ENT,
                                  target_id=str(contract_id), after={"status": "APPROVED"})
        return c

    async def reject(self, session: AsyncSession, *, actor: int, contract_id: int,
                     reason: str = "") -> MedicalStaffContract:
        c = await self._get(session, contract_id)
        inst = await _approval.get(session, doc_type=_DOC, doc_id=contract_id)
        if inst is not None and inst.get("status") == "PENDING":
            await _approval.act(session, doc_type=_DOC, doc_id=contract_id, actor=actor,
                                approve=False, note=reason)
        self._transition(c, "REJECTED")
        c.note = ((c.note or "") + f"\n[반려] {reason}").strip()
        return c

    async def activate(self, session: AsyncSession, *, actor: int | None, contract_id: int,
                       on: date | None = None) -> MedicalStaffContract:
        """발효 — APPROVED→ACTIVE(발효일 도래 검증)+기존 ACTIVE 승계. e-sign 완료 미러에서도 호출.

        승인된 계약만 발효 가능(보안검토: DRAFT 직접 발효=결재선 우회 차단).
        """
        c = await self._get(session, contract_id)
        if c.status == "ACTIVE":
            return c
        if c.status != "APPROVED":
            raise ValidationError(f"승인된 계약만 발효할 수 있습니다 ({c.status})")
        eff = c.effective_on or c.valid_from
        if (on or date.today()) < eff:
            raise ValidationError(f"발효일({eff}) 이전입니다")
        await self._supersede_active(session, c.employee_id, c.valid_from, exclude_id=c.id)
        self._transition(c, "ACTIVE")
        return c

    async def amend_pay(self, session: AsyncSession, *, actor: int, contract_id: int,
                        new_base_pay: float, effective_on: date,
                        reason: str = "") -> MedicalStaffContract:
        """기본급 변경 — 변경이력(AMOUNT) 기록 후 base_pay 갱신."""
        if new_base_pay <= 0:
            raise ValidationError("기본급은 0보다 커야 합니다")
        c = await self._get(session, contract_id)
        old = float(c.base_pay)
        session.add(ContractAmendment(
            contract_kind=_KIND, contract_id=contract_id, change_type="AMOUNT",
            old_value=str(round(old)), new_value=str(round(new_base_pay)),
            effective_on=effective_on, reason=reason or None, created_by=actor))
        c.base_pay = new_base_pay
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity=_ENT,
                                  target_id=str(contract_id),
                                  before={"base_pay": old}, after={"base_pay": new_base_pay})
        return c

    async def renew(self, session: AsyncSession, *, actor: int, contract_id: int,
                    new_valid_to: date) -> MedicalStaffContract:
        c = await self._get(session, contract_id)
        if c.valid_to is not None and new_valid_to <= c.valid_to:
            raise ValidationError("갱신 종료일은 기존 종료일 이후여야 합니다")
        old_to = c.valid_to
        session.add(ContractAmendment(
            contract_kind=_KIND, contract_id=contract_id, change_type="RENEW",
            old_value=str(old_to) if old_to else None, new_value=str(new_valid_to),
            effective_on=old_to, created_by=actor))
        c.valid_to = new_valid_to
        if c.status in ("EXPIRING", "EXPIRED"):
            c.status = "ACTIVE"
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity=_ENT,
                                  target_id=str(contract_id),
                                  before={"valid_to": str(old_to)},
                                  after={"valid_to": str(new_valid_to), "renewed": True})
        return c

    async def terminate(self, session: AsyncSession, *, actor: int, contract_id: int,
                        end_on: date | None = None, reason: str = "") -> MedicalStaffContract:
        c = await self._get(session, contract_id)
        if c.status == "TERMINATED":
            raise ValidationError("이미 해지된 계약입니다")
        end = end_on or date.today()
        c.valid_to, c.status = end, "TERMINATED"
        c.terminate_reason, c.terminated_on = (reason or None), end
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity=_ENT,
                                  target_id=str(contract_id),
                                  after={"status": "TERMINATED", "valid_to": str(end),
                                         "reason": reason})
        return c

    async def end(self, session: AsyncSession, *, actor: int, contract_id: int,
                  end_on: date) -> MedicalStaffContract:
        """현행 종료 API 호환 — TERMINATED로 위임(ENDED 용어 폐지·생애주기 일원화)."""
        return await self.terminate(session, actor=actor, contract_id=contract_id, end_on=end_on)

    async def list_for(self, session: AsyncSession, *, employee_id: int
                       ) -> list[MedicalStaffContract]:
        return list((await session.execute(
            select(MedicalStaffContract).where(MedicalStaffContract.employee_id == employee_id)
            .order_by(MedicalStaffContract.valid_from.desc()))).scalars())

    async def active(self, session: AsyncSession, *, employee_id: int
                     ) -> MedicalStaffContract | None:
        return await session.scalar(
            select(MedicalStaffContract).where(
                MedicalStaffContract.employee_id == employee_id,
                MedicalStaffContract.status == "ACTIVE"))

    async def _get(self, session: AsyncSession, contract_id: int) -> MedicalStaffContract:
        c = await session.get(MedicalStaffContract, contract_id)
        if c is None:
            raise ValidationError("존재하지 않는 계약입니다")
        return c
