"""환자 본인부담 발생주의(accrual) — 진료시점 미수금 발생·delta 전기·수금 반영 (원무 심화).

보험분(_claim_submitted: 청구시점 DR 1140/CR 4110)과 동일한 발생주의로 환자 본인부담을 계상한다.
(chart_no, care_month)별 patient_share를 DR 1131 환자미수금 / CR 4110 외래수익으로 발생하되,
누적 목표 대비 변동분(delta)만 멱등 전기(allowance_service.post 패턴). Receivable 미러로 aging·
채권원장에 노출. 수납시점 상계(DR 현금·카드/CR 1131)는 patient/service._revenue_lines(accrual=True).

config patient_accrual_enabled OFF(기본)면 전 메서드 no-op — 현행 현금주의 유지(무중단).
"""
import logging
from datetime import date, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.config import get_settings
from erp.finance.models import Receivable
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.patient.billing_service import BillingService
from erp.patient.models import Patient, Payment, Reception

ACC_PATIENT_AR = "1131"   # 환자미수금
ACC_REVENUE = "4110"      # 외래 수익

logger = logging.getLogger("erp.patient.receivable")


async def collected_as_of(session: AsyncSession, receivables: list, as_of: date) -> dict[int, float]:
    """환자 AR(source_module='patient') 각 채권의 as_of 시점 수납 복원 — 대손 aging 재구성(reconstruct=True)용.

    환자 AR 회수(CR 1131)는 일마감 전표에 일자단위 집계라 receivable별 GL 추적이 불가하고, finance는
    patient를 import 못 하므로(계층 경계), 회수 소스를 아는 patient가 재구성기를 등록한다(의존성 역전).
    source_ref=patient-ar:{chart}:{care_month} → Payment(paid_on≤as_of·care_month·PAID) 합(채권액 상한).
    """
    from sqlalchemy import tuple_
    pairs: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for r in receivables:
        parts = (r.source_ref or "").split(":")
        if len(parts) >= 3 and parts[1] and parts[2]:
            pairs.setdefault((parts[1], parts[2]), []).append((r.id, float(r.amount)))
    if not pairs:
        return {r.id: 0.0 for r in receivables}
    rows = (await session.execute(
        select(Patient.chart_no, Payment.care_month, func.coalesce(func.sum(Payment.amount), 0))
        .join(Reception, Reception.id == Payment.reception_id)
        .join(Patient, Patient.id == Reception.patient_id)
        .where(Payment.status == "PAID", Payment.paid_on <= as_of,
               tuple_(Patient.chart_no, Payment.care_month).in_(list(pairs.keys())))
        .group_by(Patient.chart_no, Payment.care_month))).all()
    coll_by_pair = {(c, cm): float(t or 0) for c, cm, t in rows}
    out: dict[int, float] = {r.id: 0.0 for r in receivables}
    for (chart, cm), rid_amts in pairs.items():
        coll = coll_by_pair.get((chart, cm), 0.0)
        for rid, amt in rid_amts:
            out[rid] = min(coll, amt)               # 과수납 clamp(음수 미수 방지·_clamp 일관)
    return out


def _month_bounds(care_month: str) -> tuple[date, date]:
    y, m = int(care_month[:4]), int(care_month[5:7])
    start = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return start, nxt


class PatientReceivableService:
    def __init__(self, vouchers: VoucherService | None = None):
        self._vs = vouchers or VoucherService()

    async def _collected(self, session: AsyncSession, *, chart_no: str,
                         care_month: str) -> float:
        """해당 chart·진료월 유효 수납(PAID) 합 — Payment.care_month 귀속(target·GL과 기준 일치).

        접수일(received_on) 창 대신 진료월 태그로 집계(귀속 불일치 해소). 취소(CANCELED)는 이미 PAID서
        제외돼 `−CANCELED` 이중차감 안 함(기수납 정합)."""
        return float(await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0))
            .join(Reception, Reception.id == Payment.reception_id)
            .join(Patient, Patient.id == Reception.patient_id)
            .where(Patient.chart_no == chart_no, Payment.status == "PAID",
                   Payment.care_month == care_month)) or 0)

    async def _gl_accrued(self, session: AsyncSession, base_ref: str) -> float:
        """해당 (chart, care_month) 환자미수금(1131) 실제 GL 순증(발생 DR − 환입 CR).

        미러(Receivable.amount)가 아닌 전표 원장에서 산출 → delta·멱등을 실제 전기상태와 결합
        (미러만 갱신되고 전표가 드롭돼 GL≠미러로 벌어지던 CRITICAL 방지)."""
        from erp.finance.models import Voucher, VoucherLine
        d, c = (await session.execute(
            select(func.coalesce(func.sum(case(
                (VoucherLine.side == "DEBIT", VoucherLine.amount), else_=0)), 0),
                   func.coalesce(func.sum(case(
                       (VoucherLine.side == "CREDIT", VoucherLine.amount), else_=0)), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code == ACC_PATIENT_AR,
                   Voucher.source_module == "patient-ar",
                   Voucher.source_ref.like(f"{base_ref}:%")))).one()
        return float(d) - float(c)

    async def accrue(self, session: AsyncSession, *, chart_no: str, care_month: str,
                     actor: int | None = None) -> dict:
        """(chart, care_month) 본인부담 발생 — 누적목표 대비 delta만 전기·Receivable 갱신(멱등)."""
        s = get_settings()
        if not s.patient_accrual_enabled:
            return {"enabled": False, "chart_no": chart_no, "care_month": care_month}
        # cutover 경계 — 기준월 이전 진료분은 현금주의로 이미 인식됨(재accrue 시 수익 이중계상)
        cutover = s.patient_accrual_cutover_month
        if cutover and care_month < cutover:
            return {"enabled": True, "skipped": "pre_cutover", "chart_no": chart_no,
                    "care_month": care_month}
        inv = await BillingService().invoice(session, chart_no=chart_no, care_month=care_month)
        target = round(float(inv.get("patient_share") or 0)) if inv.get("available") else 0
        base_ref = f"patient-ar:{chart_no}:{care_month}"
        rec = (await session.execute(
            select(Receivable).where(Receivable.source_module == "patient",
                                     Receivable.source_ref == base_ref))).scalar_one_or_none()
        # 개별 상각(WRITTEN_OFF·마이그 0209) 채권 동결 — 상각 후 billing 변동 delta가 죽은 채권을
        # 부활·재전기하지 않도록 스킵(상각 후 정정은 수동 회계 판단).
        if rec is not None and rec.status == "WRITTEN_OFF":
            return {"enabled": True, "skipped": "written_off", "chart_no": chart_no,
                    "care_month": care_month}
        # 발생 누계는 **실제 GL 1131 전기 합**에서 산출(미러 amount 아님) — 미러/GL 드리프트에도 정확.
        accrued = round(await self._gl_accrued(session, base_ref))
        delta = target - accrued
        _start, nxt = _month_bounds(care_month)
        due = nxt - timedelta(days=1)
        if delta != 0:
            # delta 전기 — source_ref를 단조 유니크(vN)로 발급. 목표 미변동(delta=0)은 위에서 스킵되므로
            # 멱등, 목표가 과거값으로 되돌아와도(target 인코딩 시 충돌·드롭되던 CRITICAL) 새 ref로 정상 전기.
            from erp.finance.models import Voucher
            seq = int(await session.scalar(
                select(func.count(Voucher.id)).where(
                    Voucher.source_module == "patient-ar",
                    Voucher.source_ref.like(f"{base_ref}:%"))) or 0) + 1
            if delta > 0:
                lines = [VoucherLineDTO(ACC_PATIENT_AR, "DEBIT", delta),
                         VoucherLineDTO(ACC_REVENUE, "CREDIT", delta)]
            else:
                lines = [VoucherLineDTO(ACC_REVENUE, "DEBIT", -delta),
                         VoucherLineDTO(ACC_PATIENT_AR, "CREDIT", -delta)]
            await self._vs.create(
                session, source_module="patient-ar", source_ref=f"{base_ref}:v{seq}",
                voucher_on=due, lines=lines, created_by=actor,
                memo=f"환자 본인부담 발생 {chart_no} {care_month} (목표 {target:,})")
        # Receivable 미러 upsert (aging·채권원장). 수금분 반영.
        collected = round(self._clamp(await self._collected(
            session, chart_no=chart_no, care_month=care_month), target))
        status = ("COLLECTED" if target > 0 and collected >= target
                  else "PARTIAL" if collected > 0 else "OUTSTANDING")
        if rec is None:
            if target > 0:
                session.add(Receivable(
                    party=chart_no, description=f"환자 본인부담 {care_month}",
                    amount=target, collected_amount=collected, billed_on=due, due_on=due,
                    status=status, source_module="patient", source_ref=base_ref))
        else:
            rec.amount, rec.collected_amount, rec.status = target, collected, status
        return {"enabled": True, "chart_no": chart_no, "care_month": care_month,
                "target": target, "accrued_before": accrued, "delta": delta,
                "collected": collected, "status": status}

    @staticmethod
    def _clamp(collected: float, target: int) -> float:
        # 과수납(타월분 등)이 본월 미수를 음수화하지 않도록 상한(채권 aging 정합).
        return max(0.0, min(collected, float(target))) if target > 0 else max(0.0, collected)

    async def accrue_all(self, session: AsyncSession, *, care_month: str,
                         actor: int | None = None) -> dict:
        """해당 진료월 BillingLine 있는 전 chart 발생 — 야간 잡용.

        ⚠ 배치와 함께 정합 이상을 확인해 결과에 실어 보낸다(단건 accrue가 아니라 여기서 한 번만
        — N+1 회피). **차단하지 않는다**: 마스터 누락 1건으로 야간 배치를 세우면 건별 격리 설계가
        무력해지고, 개통 첫날은 수납이 없는 것이 정상이라 시점 상태로는 막을 수 없다.
        대신 위반을 `warnings`로 돌려주고, 러너가 관리자 알림으로 올린다.
        """
        from erp.app.consistency_service import accrual_warnings
        s = get_settings()
        if not s.patient_accrual_enabled:
            return {"enabled": False, "care_month": care_month, "charts": 0}
        warnings = await accrual_warnings(session)
        if s.patient_accrual_cutover_month and care_month < s.patient_accrual_cutover_month:
            return {"enabled": True, "care_month": care_month, "charts": 0,
                    "skipped": "pre_cutover"}   # cutover 이전 — open_cutover가 1회 개통
        from erp.patient.models import BillingLine
        start, nxt = _month_bounds(care_month)
        charts = (await session.execute(
            select(BillingLine.chart_no).where(
                BillingLine.cancelled.is_(False),
                BillingLine.treated_on >= start, BillingLine.treated_on < nxt)
            .distinct())).scalars().all()
        # chart별 savepoint 격리 — 1건 실패(예: 마감월 late billing → VoucherService 마감 가드)가
        # 야간 잡의 전체 트랜잭션을 롤백해 병원 전 환자 발생주의가 무기한 막히던 것 방지(배치 오류격리).
        posted = failed = 0
        for chart_no in charts:
            try:
                async with session.begin_nested():
                    r = await self.accrue(session, chart_no=chart_no,
                                          care_month=care_month, actor=actor)
                if r.get("delta"):
                    posted += 1
            except Exception:   # noqa: BLE001 — 건별 격리(savepoint 롤백)·다음 chart 진행
                failed += 1
                # 로그에 환자 식별자 미기록(INV-TEL-01) — 재현은 care_month 단위 재실행으로 충분
                logger.warning("환자 발생주의 accrue 실패", extra={
                    "ctx": {"care_month": care_month}})
        return {"enabled": True, "care_month": care_month, "charts": len(charts),
                "posted": posted, "failed": failed,
                "warnings": [{"key": w["key"], "title": w["title"],
                              "summary": w["summary"]} for w in warnings]}

    async def open_cutover(self, session: AsyncSession, *, cutover_month: str,
                           actor: int | None = None) -> dict:
        """발생주의 전환 1회 개통전기 — cutover **이전** 진료월의 미수납분을 DR1131/CR4110로 인식.

        현금주의 하에서 이미 수납된 과거분은 수익(4110)이 이미 인식됐으므로 불건드림. 미수납(파이프라인)
        outstanding=본인부담−수납분만 당기 수익으로 개통(회계사 결정). source_ref로 (chart,월) 멱등 —
        재실행·부분개통 안전. 이후 그 미수의 수납은 일마감이 CR1131로 상계(전역플래그 정합).
        """
        from erp.app.consistency_service import accrual_warnings
        from erp.finance.models import Voucher
        from erp.patient.models import BillingLine
        warnings = await accrual_warnings(session)   # 개통전기 결과에도 이상을 실어 보낸다
        start, _ = _month_bounds(cutover_month)   # cutover 시작(이전 = treated_on < start)
        # 이전 진료분 (chart, 진료월) 집합
        rows = (await session.execute(
            select(BillingLine.chart_no, BillingLine.treated_on).where(
                BillingLine.cancelled.is_(False), BillingLine.treated_on < start))).all()
        cm_by_chart: dict[tuple[str, str], bool] = {}
        for chart_no, treated_on in rows:
            if chart_no and treated_on:
                cm_by_chart[(chart_no, treated_on.strftime("%Y-%m"))] = True
        opened = 0
        opened_amt = 0.0
        for (chart_no, cm) in cm_by_chart:
            inv = await BillingService().invoice(session, chart_no=chart_no, care_month=cm)
            target = round(float(inv.get("patient_share") or 0)) if inv.get("available") else 0
            if target <= 0:
                continue
            collected = round(self._clamp(await self._collected(
                session, chart_no=chart_no, care_month=cm), target))
            outstanding = target - collected
            if outstanding <= 0:
                continue
            open_ref = f"patient-ar-open:{chart_no}:{cm}"
            if await session.scalar(select(Voucher.id).where(
                    Voucher.source_module == "patient-ar",
                    Voucher.source_ref == open_ref)):
                continue   # 멱등 — 이미 개통
            await self._vs.create(
                session, source_module="patient-ar", source_ref=open_ref,
                voucher_on=start - timedelta(days=1), created_by=actor,
                lines=[VoucherLineDTO(ACC_PATIENT_AR, "DEBIT", outstanding),
                       VoucherLineDTO(ACC_REVENUE, "CREDIT", outstanding)],
                memo=f"발생주의 개통 {chart_no} {cm} (미수 {outstanding:,})")
            opened += 1
            opened_amt += outstanding
        return {"cutover_month": cutover_month, "opened": opened,
                "opened_amount": round(opened_amt),
                "warnings": [{"key": w["key"], "title": w["title"],
                              "summary": w["summary"]} for w in warnings]}
