"""환자 미수금 회수 워크플로 — 독촉·분납·개별 대손상각 (내부운영 갭 감사 원무 ①·마이그 0209).

발생주의(receivable_service)·aging·ECL 충당(allowance_service)은 구축돼 있으나 회수 루프가 "충당금
설정"에서 끊기던 갭 보완. 대상 = finance.Receivable(source_module='patient') 환자 미러.

개별 상각 회계(관행): 회수불능 확정 시 충당금 우선 상계 — DR 대손충당금(1190) min(충당잔액, 미수잔액)
+ 부족분 DR 대손상각비(5690) / CR 환자미수금(1131). 상각 후 채권은 aging·ECL 대상 제외(상각일 이후).
전표 source_module='patient-ar-wo'(발생주의 'patient-ar'와 분리 — _gl_accrued delta 재전기 충돌 방지).
"""
from datetime import date
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.allowance_service import ACC_ALLOWANCE, ACC_BADDEBT, AllowanceService
from erp.finance.models import Receivable
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.patient.models import DunningNotice, InstallmentPlan
from erp.patient.receivable_service import ACC_PATIENT_AR

_PLAN_TRANSITIONS = {"ACTIVE": {"COMPLETED", "DEFAULTED", "CANCELED"}}


def _outstanding(rec: Receivable) -> float:
    return float(rec.amount) - float(rec.collected_amount)


class PatientCollectionService:
    def __init__(self, vouchers: VoucherService | None = None):
        self._vs = vouchers or VoucherService()

    async def _patient_receivable(self, session: AsyncSession, receivable_id: int) -> Receivable:
        rec = await session.get(Receivable, receivable_id)
        if rec is None or rec.source_module != "patient":
            raise ValidationError("환자 미수 채권이 아닙니다", {"receivable_id": receivable_id})
        return rec

    async def overdue_list(self, session: AsyncSession, *, as_of: date,
                           min_days: int = 1, limit: int = 500) -> dict:
        """기한 경과 환자 미수 — 독촉 차수·분납 여부 병기(회수 작업 목록).

        성능: 경과일 필터를 SQL(due_on ≤ as_of − min_days)로 밀어 due_on 인덱스 범위 스캔·limit
        (파이썬 전건 로드 후 필터 제거). due_on 오름차순=가장 오래 연체된 것 우선.
        """
        from datetime import timedelta
        cutoff = as_of - timedelta(days=min_days)
        rows = (await session.scalars(
            select(Receivable).where(
                Receivable.source_module == "patient",
                Receivable.status.in_(("OUTSTANDING", "PARTIAL")),
                Receivable.due_on <= cutoff)   # 경과일 필터 SQL 이관(sargable)
            .order_by(Receivable.due_on).limit(limit))).all()
        rids = [r.id for r in rows]
        last_dunning: dict = {}
        active_plans: set = set()
        if rids:
            for rid, lvl, sent in (await session.execute(
                    select(DunningNotice.receivable_id, func.max(DunningNotice.level),
                           func.max(DunningNotice.sent_on))
                    .where(DunningNotice.receivable_id.in_(rids))
                    .group_by(DunningNotice.receivable_id))).all():
                last_dunning[rid] = {"level": int(lvl), "sent_on": sent.isoformat()}
            active_plans = set((await session.scalars(
                select(InstallmentPlan.receivable_id).where(
                    InstallmentPlan.receivable_id.in_(rids),
                    InstallmentPlan.status == "ACTIVE"))).all())
        items = [{
            "receivable_id": r.id, "chart_no": r.party, "description": r.description,
            "amount": float(r.amount), "collected": float(r.collected_amount),
            "outstanding": _outstanding(r), "due_on": r.due_on.isoformat(),
            "days_overdue": (as_of - r.due_on).days, "status": r.status,
            "dunning": last_dunning.get(r.id), "has_active_plan": r.id in active_plans,
        } for r in rows]
        items.sort(key=lambda x: x["outstanding"], reverse=True)
        return {"as_of": as_of.isoformat(), "count": len(items),
                "total_outstanding": sum(i["outstanding"] for i in items), "items": items}

    async def record_dunning(self, session: AsyncSession, *, receivable_id: int,
                             method: str = "PHONE", note: str | None = None,
                             sent_on: date | None = None,
                             actor: int | None = None) -> DunningNotice:
        """독촉(납부안내) 기록 — 차수 자동 증가(1차→2차→…)."""
        rec = await self._patient_receivable(session, receivable_id)
        if rec.status in ("COLLECTED", "WRITTEN_OFF"):
            raise ValidationError("종결된 채권은 독촉할 수 없습니다", {"status": rec.status})
        outstanding = _outstanding(rec)
        if outstanding <= 0:
            raise ValidationError("미수 잔액이 없습니다")
        prev = int(await session.scalar(
            select(func.coalesce(func.max(DunningNotice.level), 0))
            .where(DunningNotice.receivable_id == receivable_id)) or 0)
        notice = DunningNotice(
            receivable_id=receivable_id, chart_no=rec.party, level=prev + 1,
            method=method, outstanding=outstanding, note=note,
            sent_on=sent_on or date.today(), created_by=actor)
        session.add(notice)
        await session.flush()
        return notice

    async def dunning_history(self, session: AsyncSession, *, receivable_id: int) -> list[dict]:
        rows = (await session.scalars(
            select(DunningNotice).where(DunningNotice.receivable_id == receivable_id)
            .order_by(DunningNotice.level))).all()
        return [{"id": n.id, "level": n.level, "method": n.method,
                 "outstanding": float(n.outstanding), "note": n.note,
                 "sent_on": n.sent_on.isoformat(), "created_by": n.created_by} for n in rows]

    async def create_plan(self, session: AsyncSession, *, receivable_id: int, months: int,
                          start_month: str, note: str | None = None,
                          actor: int | None = None) -> InstallmentPlan:
        """분납(납부계획) 합의 — 합의 시점 잔액을 months 회차로. 채권당 ACTIVE 1건."""
        if not (2 <= months <= 36):
            raise ValidationError("분납 회차는 2~36 사이여야 합니다", {"months": months})
        rec = await self._patient_receivable(session, receivable_id)
        if rec.status in ("COLLECTED", "WRITTEN_OFF"):
            raise ValidationError("종결된 채권은 분납할 수 없습니다", {"status": rec.status})
        outstanding = _outstanding(rec)
        if outstanding <= 0:
            raise ValidationError("미수 잔액이 없습니다")
        exists = await session.scalar(
            select(InstallmentPlan.id).where(InstallmentPlan.receivable_id == receivable_id,
                                             InstallmentPlan.status == "ACTIVE"))
        if exists:
            raise ValidationError("이미 진행 중인 분납 계획이 있습니다", {"plan_id": exists})
        plan = InstallmentPlan(
            receivable_id=receivable_id, chart_no=rec.party, total_amount=outstanding,
            months=months, monthly_amount=ceil(outstanding / months),
            start_month=start_month, note=note, created_by=actor)
        session.add(plan)
        await session.flush()
        return plan

    async def update_plan_status(self, session: AsyncSession, *, plan_id: int,
                                 status: str) -> InstallmentPlan:
        """분납 상태 전이 — ACTIVE→COMPLETED/DEFAULTED/CANCELED만(종결 후 불변)."""
        # 행잠금 — 동시 전이 경합 시 둘 다 ACTIVE를 읽고 각자 종결시키는 것 차단(정합성 하드닝)
        plan = await session.get(InstallmentPlan, plan_id, with_for_update=True)
        if plan is None:
            raise ValidationError("존재하지 않는 분납 계획입니다")
        allowed = _PLAN_TRANSITIONS.get(plan.status, set())
        if status not in allowed:
            raise ValidationError("허용되지 않는 분납 상태 전이입니다",
                                  {"from": plan.status, "to": status,
                                   "allowed": sorted(allowed)})
        plan.status = status
        return plan

    async def write_off(self, session: AsyncSession, *, receivable_id: int, reason: str,
                        on: date | None = None, actor: int | None = None) -> dict:
        """개별 대손상각 — 충당금(1190) 우선 상계·부족분 5690 / CR 1131. 멱등(wo:{id})."""
        rec = await self._patient_receivable(session, receivable_id)
        if rec.status == "WRITTEN_OFF":
            return {"receivable_id": receivable_id, "already": True,
                    "written_off_on": rec.written_off_on.isoformat()
                    if rec.written_off_on else None}
        if rec.status == "COLLECTED":
            raise ValidationError("회수 완료 채권은 상각할 수 없습니다")
        if not (reason or "").strip():
            raise ValidationError("상각 사유는 필수입니다")
        outstanding = round(_outstanding(rec))
        if outstanding <= 0:
            raise ValidationError("미수 잔액이 없습니다")
        on = on or date.today()
        # 충당금 상계 우선 — 잔액 범위 내(음수 방어)
        allowance = max(0.0, await AllowanceService()._allowance_balance(session, as_of=on))
        use_allow = round(min(allowance, outstanding))
        rest = outstanding - use_allow
        lines = []
        if use_allow > 0:
            lines.append(VoucherLineDTO(ACC_ALLOWANCE, "DEBIT", use_allow))
        if rest > 0:
            lines.append(VoucherLineDTO(ACC_BADDEBT, "DEBIT", rest))
        lines.append(VoucherLineDTO(ACC_PATIENT_AR, "CREDIT", outstanding))
        v = await self._vs.create(
            session, source_module="patient-ar-wo", source_ref=f"wo:{receivable_id}",
            voucher_on=on, lines=lines, created_by=actor,
            memo=f"환자 미수 개별상각 {rec.party} ({reason})")
        rec.status = "WRITTEN_OFF"
        rec.written_off_on = on
        rec.write_off_reason = reason[:200]
        return {"receivable_id": receivable_id, "written_off": outstanding,
                "allowance_used": use_allow, "bad_debt_expense": rest,
                "voucher_id": v.id, "written_off_on": on.isoformat()}
