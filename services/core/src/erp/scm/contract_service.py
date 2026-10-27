"""위수탁 계약 서비스 — 생애주기(DRAFT→승인→발효→만기→갱신/해지)·월 위탁료(멱등)·변경이력.

상태머신: DRAFT→PENDING_APPROVAL→APPROVED→ACTIVE→(EXPIRING)→EXPIRED|RENEWED|TERMINATED|REJECTED.
register는 기본 ACTIVE(현행 호환), require_approval=True면 DRAFT 결재 경로. 전이는 공통 커널(common.contract_lifecycle) 가드.
"""
import re
from calendar import monthrange
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.approval_engine import ApprovalEngine
from erp.common.contract_lifecycle import ACCRUABLE, EXPIRING_SOON_DAYS, assert_transition
from erp.common.models import ContractAmendment
from erp.errors import ValidationError
from erp.finance.models import Payable
from erp.scm.models import OutsourcingContract

_audit = AuditService()
_approval = ApprovalEngine()
_KIND = "OUTSOURCING"
_DOC = "OUTSOURCING_CONTRACT"
# 위탁료 비용계정 — 2026-10-09 전체 흐름 검토 E(월 위탁료 GL 미전기 해소). 기존 계정 마스터 재사용:
# 5510 관리비용(기본), IT 계약은 5525 정보시스템운영비(ITAM 비용 GL과 동일 계정).
ACC_OUTSOURCING_EXP = "5510"
_EXPENSE_BY_CATEGORY = {"IT": "5525"}


# 2026-10-09 전체 흐름 검토 2·3차 E: 만료(EXPIRED)·해지(TERMINATED) 계약도 **계약기간(해지일까지) 안의
# 월**은 위탁료를 발생시킬 수 있다 — 일 배치가 EXPIRED로 바꾸거나 월중 해지되면 최종월(또는 놓친 과거월)
# 위탁료가 영영 누락됐다. 해지일 이후 월은 in_contract_period가 막고, 같은 달 중복은 contract:{id}:{yyyymm}
# 멱등이 막는다. 미발효(DRAFT/PENDING_APPROVAL/APPROVED)·반려(REJECTED)는 불가.
MONTH_END_ACCRUABLE = frozenset(ACCRUABLE | {"EXPIRED", "TERMINATED"})


def accruable_for_month(c: OutsourcingContract, month_start: date, month_end: date) -> bool:
    """월 위탁료 발생 가능 여부(상태+기간) — 월마감 필터·accrue_month 공용(2차 E)."""
    if c.status in ACCRUABLE:
        return in_contract_period(c, month_start, month_end)
    if c.status in ("EXPIRED", "TERMINATED"):
        # 계약기간 안의 달이면 허용 — 마지막 달만 허용하면, 11/3 해지 후 11/5에 10월 월마감을 돌릴 때
        # 계약이 10월 내내 유효했는데도 10월분이 영구히 빠졌다(3차 검토 E). 이중 발생은 source_ref 멱등.
        return in_contract_period(c, month_start, month_end)
    return False


def in_contract_period(c: OutsourcingContract, month_start: date, month_end: date) -> bool:
    """해당 월이 계약기간(start_on~end_on, 해지 시 terminated_on까지)과 겹치는지 — 월마감 필터 공용."""
    last = min(c.end_on, c.terminated_on) if c.terminated_on else c.end_on
    return c.start_on <= month_end and last >= month_start


class OutsourcingContractService:
    async def register(self, session: AsyncSession, *, actor: int, vendor_name: str,
                       category: str, title: str, monthly_amount: float,
                       start_on: date, end_on: date, supplier_id: int | None = None,
                       auto_renew: bool = False, require_approval: bool = False,
                       contract_type: str = "OUTSOURCING", billing_type: str = "MONTHLY",
                       note: str | None = None, policy_no: str | None = None,
                       coverage_limit: float | None = None,
                       premium_cycle: str | None = None) -> OutsourcingContract:
        if end_on <= start_on:
            raise ValidationError("계약 종료일은 시작일 이후여야 합니다")
        if monthly_amount < 0:
            raise ValidationError("계약금액은 음수일 수 없습니다")
        if category not in ("MEAL", "LAUNDRY", "WASTE", "SECURITY", "CLEANING",
                            "FACILITY", "IT", "INSURANCE", "OTHER"):
            raise ValidationError(f"계약 분류 오류: {category}")   # DB CHECK 전에 명확한 메시지
        if contract_type not in ("OUTSOURCING", "PURCHASE", "SERVICE", "LEASE",
                                 "INSURANCE", "OTHER"):
            raise ValidationError(f"계약 유형 오류: {contract_type}")
        if billing_type not in ("MONTHLY", "ONE_TIME"):
            raise ValidationError(f"청구 유형 오류: {billing_type}")
        if premium_cycle is not None and premium_cycle not in (
                "ANNUAL", "SEMIANNUAL", "QUARTERLY", "MONTHLY"):
            raise ValidationError(f"납입주기 오류: {premium_cycle}")
        # 결재선 룰이 매칭되면 승인 경로(DRAFT) 강제 — 직접 ACTIVE 생성으로 결재선 우회 방지(보안검토).
        if not require_approval and await _approval.find_rule(
                session, doc_type=_DOC, amount=float(monthly_amount), category=category):
            require_approval = True
        c = OutsourcingContract(
            vendor_name=vendor_name, supplier_id=supplier_id, category=category,
            contract_type=contract_type, billing_type=billing_type,
            title=title, monthly_amount=monthly_amount, start_on=start_on,
            end_on=end_on, effective_on=start_on, auto_renew=auto_renew, note=note,
            policy_no=policy_no,
            coverage_limit=round(coverage_limit) if coverage_limit is not None else None,
            premium_cycle=premium_cycle,
            status="DRAFT" if require_approval else "ACTIVE", created_by=actor)
        session.add(c)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="scm.outsourcing_contracts", target_id=str(c.id),
                                  after={"vendor": vendor_name, "title": title,
                                         "monthly": monthly_amount, "status": c.status})
        return c

    def _transition(self, c: OutsourcingContract, to: str) -> None:
        assert_transition(c.status, to)
        c.status = to

    async def submit_for_approval(self, session: AsyncSession, *, actor: int,
                                  contract_id: int) -> OutsourcingContract:
        c = await self._get(session, contract_id)
        self._transition(c, "PENDING_APPROVAL")
        # 결재선 룰 있으면 다단계 생성(없으면 단일 승인으로 폴백)
        await _approval.build(session, doc_type=_DOC, doc_id=contract_id,
                              amount=float(c.monthly_amount), created_by=c.created_by or actor,
                              category=c.category)
        return c

    async def approve(self, session: AsyncSession, *, actor: int, contract_id: int,
                      override: bool = False) -> OutsourcingContract:
        """승인 — 결재선 있으면 다단계(완주 시 APPROVED), 없으면 레거시 단일 승인(직무분리).

        override=True(최고경영자 전권): DIRECTOR가 결재선 건너뛰어 즉시 확정(직무분리 유지).
        """
        c = await self._get(session, contract_id)
        inst = await _approval.get(session, doc_type=_DOC, doc_id=contract_id)
        if (inst is None or inst.get("status") != "PENDING") and c.status in (
                "DRAFT", "PENDING_APPROVAL"):
            # 2026-10-09 전체 흐름 검토 E: 상신(submit) 없이 DRAFT에서 바로 approve하면 결재선이
            # 한 번도 생성되지 않아 룰이 매칭돼도 '레거시 단일 승인'으로 빠졌다(다단계 결재 우회).
            # PO의 _ensure_chain과 같이 승인 시점에도 룰로 결재선을 만들고, 만들어지면 그 결재선을 탄다.
            built = await _approval.build(session, doc_type=_DOC, doc_id=contract_id,
                                          amount=float(c.monthly_amount),
                                          created_by=c.created_by or actor,
                                          category=c.category)
            if built is not None:
                if c.status == "DRAFT":
                    self._transition(c, "PENDING_APPROVAL")
                inst = {"status": "PENDING"}
        if inst is not None and inst.get("status") == "PENDING":
            out = await _approval.act(session, doc_type=_DOC, doc_id=contract_id, actor=actor,
                                      approve=True, override=override)
            if out.status != "APPROVED":
                return c                       # 다음 단계 대기(PENDING_APPROVAL 유지)
        elif c.created_by == actor:            # 레거시 직무분리
            raise ValidationError("직무분리 — 작성자 본인은 계약을 승인할 수 없습니다")
        self._transition(c, "APPROVED")
        c.approved_by, c.approved_at = actor, datetime.now(timezone.utc)
        await _audit.record_audit(session, actor=actor, action="APPROVE",
                                  entity="scm.outsourcing_contracts", target_id=str(contract_id),
                                  after={"status": "APPROVED"})
        return c

    async def reject(self, session: AsyncSession, *, actor: int, contract_id: int,
                     reason: str = "") -> OutsourcingContract:
        c = await self._get(session, contract_id)
        inst = await _approval.get(session, doc_type=_DOC, doc_id=contract_id)
        if inst is not None and inst.get("status") == "PENDING":
            await _approval.act(session, doc_type=_DOC, doc_id=contract_id, actor=actor,
                                approve=False, note=reason)
        self._transition(c, "REJECTED")
        c.note = ((c.note or "") + f"\n[반려] {reason}").strip()
        return c

    async def activate(self, session: AsyncSession, *, actor: int | None, contract_id: int,
                       on: date | None = None) -> OutsourcingContract:
        """발효 — APPROVED→ACTIVE(발효일 도래 검증). e-sign 완료 미러에서도 호출.

        승인된 계약만 발효 가능(보안검토: DRAFT 직접 발효=결재선 우회 차단).
        """
        c = await self._get(session, contract_id)
        if c.status == "ACTIVE":
            return c
        if c.status != "APPROVED":
            raise ValidationError(f"승인된 계약만 발효할 수 있습니다 ({c.status})")
        eff = c.effective_on or c.start_on
        if (on or date.today()) < eff:
            raise ValidationError(f"발효일({eff}) 이전입니다")
        self._transition(c, "ACTIVE")
        return c

    async def amend_amount(self, session: AsyncSession, *, actor: int, contract_id: int,
                           new_monthly: float, effective_on: date,
                           reason: str = "") -> OutsourcingContract:
        """월 위탁료 변경 — 변경이력(AMOUNT) 기록 후 단가 갱신(이후 accrue는 변경액 적용)."""
        if new_monthly < 0:
            raise ValidationError("월 계약금액은 음수일 수 없습니다")
        c = await self._get(session, contract_id)
        old = float(c.monthly_amount)
        session.add(ContractAmendment(
            contract_kind=_KIND, contract_id=contract_id, change_type="AMOUNT",
            old_value=str(round(old)), new_value=str(round(new_monthly)),
            effective_on=effective_on, reason=reason or None, created_by=actor))
        c.monthly_amount = new_monthly
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="scm.outsourcing_contracts", target_id=str(contract_id),
                                  before={"monthly": old}, after={"monthly": new_monthly})
        return c

    async def renew(self, session: AsyncSession, *, actor: int, contract_id: int,
                    new_end_on: date) -> OutsourcingContract:
        c = await self._get(session, contract_id)
        if c.status not in ("ACTIVE", "EXPIRING", "EXPIRED"):   # 상태머신 우회 차단 — TERMINATED/DRAFT/미승인 부활 금지(EXPIRED만 갱신 부활)
            raise ValidationError(f"갱신 가능 상태가 아닙니다 ({c.status})")
        if new_end_on <= c.end_on:
            raise ValidationError("갱신 종료일은 기존 종료일 이후여야 합니다")
        old_end = c.end_on
        session.add(ContractAmendment(
            contract_kind=_KIND, contract_id=contract_id, change_type="RENEW",
            old_value=str(old_end), new_value=str(new_end_on), effective_on=old_end,
            created_by=actor))
        c.end_on, c.status = new_end_on, "ACTIVE"      # 갱신 시 ACTIVE 복원(만료/임박→ACTIVE)
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="scm.outsourcing_contracts", target_id=str(contract_id),
                                  before={"end_on": str(old_end)},
                                  after={"end_on": str(new_end_on), "renewed": True})
        return c

    async def terminate(self, session: AsyncSession, *, actor: int, contract_id: int,
                        reason: str = "") -> OutsourcingContract:
        c = await self._get(session, contract_id)
        if c.status == "TERMINATED":
            raise ValidationError("이미 해지된 계약입니다")
        self._transition(c, "TERMINATED")   # 상태머신 커널 경유 — 미발효(DRAFT/PENDING/APPROVED) 계약 해지 우회 차단(반려 경로 사용)
        c.terminate_reason, c.terminated_on = (reason or None), date.today()
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="scm.outsourcing_contracts", target_id=str(contract_id),
                                  after={"terminated": True, "reason": reason})
        return c

    async def run_lifecycle_transitions(self, session: AsyncSession, *,
                                        today: date | None = None) -> dict:
        """일 배치 — ACTIVE/EXPIRING 계약의 만기 자동 전이. 표시로직을 실 상태전이로 승격.

        ACTIVE + 0≤d_day≤60 → EXPIRING. ACTIVE/EXPIRING + d_day<0 → auto_renew면 1년 갱신,
        아니면 EXPIRED. 멱등(이미 목표 상태면 무변경)·시간대 무관(date 기준).
        """
        today = today or date.today()
        rows = (await session.scalars(select(OutsourcingContract).where(
            OutsourcingContract.status.in_(("ACTIVE", "EXPIRING"))))).all()
        marked_expiring = renewed = expired = 0
        for c in rows:
            d_day = (c.end_on - today).days
            if d_day < 0:
                if c.auto_renew:
                    y2 = c.end_on.year + 1   # 윤년 2/29 보정 — 비윤년 대상은 말일로 클램프
                    new_end = date(y2, c.end_on.month,
                                   min(c.end_on.day, monthrange(y2, c.end_on.month)[1]))
                    await self.renew(session, actor=0, contract_id=c.id, new_end_on=new_end)
                    renewed += 1
                else:
                    self._transition(c, "EXPIRED")
                    expired += 1
            elif c.status == "ACTIVE" and d_day <= EXPIRING_SOON_DAYS:
                self._transition(c, "EXPIRING")
                marked_expiring += 1
        return {"expiring": marked_expiring, "renewed": renewed, "expired": expired}

    async def _amount_as_of(self, session: AsyncSession, c: OutsourcingContract,
                            on: date) -> float:
        """on 시점에 유효한 월 금액 — AMOUNT 변경이력의 effective_on 기준(as-of).

        2026-10-09 전체 흐름 검토 E: amend_amount가 monthly_amount를 즉시 덮어써서, 7/1 발효 변경을
        6월에 등록하면 아직 accrue 안 한 5·6월분도 새 금액으로 발생했다(effective_on 무시).
        이력이 없으면 현재 단가. 발효일 이전 월이면 가장 이른 변경의 old_value(원 단가).
        """
        amends = (await session.scalars(select(ContractAmendment).where(
            ContractAmendment.contract_kind == _KIND,
            ContractAmendment.contract_id == c.id,
            ContractAmendment.change_type == "AMOUNT")
            .order_by(ContractAmendment.effective_on, ContractAmendment.id))).all()
        amends = [a for a in amends if a.effective_on is not None]
        if not amends:
            return float(c.monthly_amount)
        applied = [a for a in amends if a.effective_on <= on]
        if applied:
            return float(applied[-1].new_value)
        return float(amends[0].old_value)

    async def accrue_month(self, session: AsyncSession, *, actor: int, contract_id: int,
                           yyyymm: str) -> dict:
        """월 위탁료 미지급 발생 — (계약, 월) 멱등. 익월 10일 지급기일. ACTIVE/EXPIRING만.

        2026-10-09 전체 흐름 검토 E: 종전엔 Payable 행만 만들고 전표가 없어 비용 미인식 + 지급 시
        DR 2110만 찍혀 미지급금 GL이 음수가 됐다. SettlementService.record_payable 경유로
        DR 비용 / CR 2110 전표를 함께 만들어 보조원장(Payable)과 GL을 일치시킨다.
        또한 계약기간(start_on~end_on)·해지일 밖의 월은 발생 금지, 금액은 변경이력 as-of.
        """
        if not re.fullmatch(r"\d{4}-\d{2}", yyyymm):
            raise ValidationError("yyyymm 형식 오류 (YYYY-MM)")
        c = await self._get(session, contract_id)
        if c.status not in MONTH_END_ACCRUABLE:
            raise ValidationError(f"활성 계약이 아닙니다 ({c.status})")
        if c.billing_type != "MONTHLY":
            raise ValidationError("월 발생 대상이 아닙니다 (일회성 계약)")
        y, m = int(yyyymm[:4]), int(yyyymm[5:7])
        if not 1 <= m <= 12:
            raise ValidationError("yyyymm 형식 오류 (YYYY-MM)")
        month_start = date(y, m, 1)
        month_end = date(y, m, monthrange(y, m)[1])
        if not in_contract_period(c, month_start, month_end):
            raise ValidationError(
                f"계약기간({c.start_on}~{c.terminated_on or c.end_on}) 밖의 월은 "
                f"위탁료를 발생시킬 수 없습니다 ({yyyymm})")
        ref = f"contract:{contract_id}:{yyyymm}"
        existing = await session.scalar(select(Payable).where(
            Payable.source_module == "contract", Payable.source_ref == ref))
        if existing is not None:
            return {"payable_id": existing.id, "duplicate": True}
        amount = await self._amount_as_of(session, c, month_start)
        if amount <= 0:
            # 0원 전표는 차대 0 라인이라 무의미 — 발생 없음으로 명시(무음 0원 채무 방지)
            return {"payable_id": None, "duplicate": False, "skipped": True, "amount": 0.0}
        due = date(y + (m == 12), (m % 12) + 1, 10)
        from erp.finance.settlement_service import SettlementService
        ap = await SettlementService().record_payable(
            session, party=c.vendor_name, description=f"{c.title} {yyyymm} 위탁료",
            amount=amount, incurred_on=month_start, due_on=due,
            expense_account=_EXPENSE_BY_CATEGORY.get(c.category, ACC_OUTSOURCING_EXP),
            source_module="contract", source_ref=ref, created_by=actor or None,
            party_ref=f"supplier:{c.supplier_id}" if c.supplier_id else None)
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="finance.payables", target_id=str(ap.id),
                                  after={"contract": contract_id, "yyyymm": yyyymm,
                                         "amount": amount})
        return {"payable_id": ap.id, "duplicate": False,
                "amount": amount, "due_on": due}

    async def list(self, session: AsyncSession, *, today: date | None = None) -> dict:
        today = today or date.today()
        rows = (await session.scalars(
            select(OutsourcingContract).order_by(OutsourcingContract.end_on))).all()
        items, monthly_total = [], 0.0
        by_category: dict[str, float] = {}
        for c in rows:
            d_day = (c.end_on - today).days
            state = c.status
            # 표시 보정 — 배치 전이 전이라도 ACTIVE/EXPIRING이 만기 경과면 EXPIRED로 표시(저장 미변경)
            if state in ("ACTIVE", "EXPIRING") and d_day < 0:
                state = "EXPIRED"
            elif state == "ACTIVE" and 0 <= d_day <= EXPIRING_SOON_DAYS:
                state = "EXPIRING"
            # 월 정기 위탁료 합계는 MONTHLY 정액 계약만(ONE_TIME 일회성은 제외)
            if state in ACCRUABLE and c.billing_type == "MONTHLY":
                monthly_total += float(c.monthly_amount)
                by_category[c.category] = (by_category.get(c.category, 0.0)
                                           + float(c.monthly_amount))
            items.append({"id": c.id, "vendor_name": c.vendor_name,
                          "category": c.category, "title": c.title,
                          "contract_type": c.contract_type, "billing_type": c.billing_type,
                          "monthly_amount": float(c.monthly_amount),
                          "start_on": c.start_on, "end_on": c.end_on,
                          "effective_on": c.effective_on,
                          "d_day": d_day, "auto_renew": c.auto_renew,
                          "status": state,
                          "sign_status": c.sign_status,
                          "sign_envelope_id": c.sign_envelope_id,
                          "sign_doc_attachment_id": c.sign_doc_attachment_id,
                          "policy_no": c.policy_no,
                          "coverage_limit": (float(c.coverage_limit)
                                             if c.coverage_limit is not None else None),
                          "premium_cycle": c.premium_cycle,
                          "expiring_soon": state == "EXPIRING"})
        return {"items": items, "monthly_total": monthly_total,
                "by_category": [{"category": k, "monthly": v}
                                for k, v in sorted(by_category.items())],
                "expiring_soon": [i for i in items if i["expiring_soon"]]}

    _TYPE_LABEL = {"OUTSOURCING": "위수탁계약", "PURCHASE": "구매계약",
                   "SERVICE": "용역계약", "LEASE": "임대계약", "INSURANCE": "보험계약",
                   "OTHER": "기타계약"}

    async def render_pdf(self, session: AsyncSession, *, contract_id: int, org_name: str,
                         generated_on: date | None = None) -> bytes:
        """계약서(안) PDF — 계약 정보 + 변경이력. 정본은 e-sign 별도(본 문서는 안/기록용)."""
        from erp.common.org_service import org_logo_data_uri
        from erp.common.pdf import render_pdf as _render
        c = await self._get(session, contract_id)
        amends = (await session.scalars(select(ContractAmendment).where(
            ContractAmendment.contract_kind == _KIND,
            ContractAmendment.contract_id == contract_id)
            .order_by(ContractAmendment.id))).all()
        ctx = {
            "title": c.title, "contract_no": f"OC-{c.id}",
            "type_label": self._TYPE_LABEL.get(c.contract_type, c.contract_type),
            "status": c.status, "vendor_name": c.vendor_name, "category": c.category,
            "start_on": c.start_on, "end_on": c.end_on, "effective_on": c.effective_on,
            "amount": float(c.monthly_amount), "auto_renew": c.auto_renew,
            "amount_label": "월 금액" if c.billing_type == "MONTHLY" else "계약 총액",
            "terms": c.note or "별도 첨부 계약 조건에 따른다.",
            "amendments": [{"change_type": a.change_type, "old_value": a.old_value,
                            "new_value": a.new_value, "effective_on": a.effective_on}
                           for a in amends],
            "org_name": org_name, "sign_status": c.sign_status,
            "logo": await org_logo_data_uri(session),   # 기관 로고(첨부)
            "generated_on": (generated_on or date.today()).isoformat(),
        }
        return _render("contract.html", ctx)

    async def _get(self, session: AsyncSession, contract_id: int) -> OutsourcingContract:
        c = await session.get(OutsourcingContract, contract_id)
        if c is None:
            raise ValidationError("존재하지 않는 계약입니다")
        return c
