"""성과급 산정·승인 (P4-3/4) — 계약×실적 산정 + 등록→검토→승인(급여·전표)→취소(claw-back).

doctor_id == employee.id 가정(결정사항). 승인 시 PayrollLine(INCENTIVE) + 미지급 전표(급여비용/
미지급금, 발생주의). 취소는 역분개 + 라인 제거(지급 전). 보수=민감 → 라우터 RBAC + 감사.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.months import require_ym
from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.hr.contract_service import MedicalContractService
from erp.hr.incentive_engine import calculate
from erp.hr.models import (
    Employee, IncentiveCalculation, MedicalStaffContract, PayrollLine, PayrollRecord,
)
from erp.hr.physician_revenue import PhysicianRevenueService

_contracts = MedicalContractService()
_revenue = PhysicianRevenueService()
_vouchers = VoucherService()
_audit = AuditService()
ACC_SALARY_EXP = "5310"   # 급여비용
ACC_AP = "2110"           # 미지급금
ACC_WITHHOLDING = "2150"  # 예수금(성과급 원천징수 — FLAT/COMBINED)
# 승인 워크플로가 소유하는 급여 라인 — employee_service._EXTERNAL_LINE_CODES와 동일 집합
_EXTERNAL_CODES = ("INCENTIVE", "INCTAX", "INCLOCALTAX")


def _period_end(period: str) -> date:
    y, m = int(period[:4]), int(period[5:7])
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return nxt - timedelta(days=1)


class IncentiveService:
    async def calculate(self, session: AsyncSession, *, employee_id: int, period: str) -> dict:
        """성과급 산정(읽기) — 계약 산식 × 실적. 등록·승인 없이 미리보기."""
        period = require_ym(period)
        contract = await _contracts.active(session, employee_id=employee_id)
        if contract is None:
            return {"employee_id": employee_id, "period": period, "contract": None,
                    "amount": 0, "net_revenue": 0, "note": "ACTIVE 의료진 계약 없음"}
        rev = await _revenue.aggregate(session, period=period, doctor_id=employee_id)
        net = rev["rows"][0]["net_revenue"] if rev["rows"] else 0.0
        # 인사평가 등급 배율(P1-1) — 설정 옵트인 시 해당 연도 확정 등급을 성과급에 반영
        eval_grade = await self._eval_grade(session, employee_id, period)
        result = calculate(
            incentive_type=contract.incentive_type,
            incentive_rate=float(contract.incentive_rate) if contract.incentive_rate
            is not None else None, params=contract.incentive_params, net_revenue=net,
            eval_grade=eval_grade)
        return {"employee_id": employee_id, "period": period, "contract_id": contract.id,
                "revenue": rev["rows"][0] if rev["rows"] else None, **result}

    async def _eval_grade(self, session: AsyncSession, employee_id: int, period: str) -> str | None:
        """설정(incentive_grade_link) ON 시 해당 연도 최신 확정 인사평가 등급. OFF/없음=None(무영향)."""
        from erp.hr.models import EvalCycle, Evaluation
        from erp.hr.payroll_settings import PayrollSettingsService
        cfg = await PayrollSettingsService().effective(session)
        if not cfg.get("incentive_grade_link"):
            return None
        year = period[:4]
        ev = await session.scalar(
            select(Evaluation).join(EvalCycle, EvalCycle.id == Evaluation.cycle_id)
            .where(Evaluation.employee_id == employee_id, Evaluation.status == "CONFIRMED",
                   Evaluation.final_grade.isnot(None), EvalCycle.period.like(f"{year}%"))
            .order_by(Evaluation.confirmed_at.desc()))
        return ev.final_grade if ev else None

    async def register(self, session: AsyncSession, *, actor: int, employee_id: int,
                       period: str) -> IncentiveCalculation:
        """산정 등록(DRAFT) — 근거 snapshot 보존. 기존 DRAFT/REVIEWED는 갱신, APPROVED는 거부."""
        period = require_ym(period)
        calc_data = await self.calculate(session, employee_id=employee_id, period=period)
        existing = await session.scalar(select(IncentiveCalculation).where(
            IncentiveCalculation.employee_id == employee_id,
            IncentiveCalculation.period == period))
        if existing and existing.status == "APPROVED":
            raise ValidationError("이미 승인된 성과급입니다 — 취소 후 재산정")
        snap = {k: calc_data.get(k) for k in
                ("incentive_type", "rate_pct", "net_revenue", "raw_amount", "revenue",
                 "eval_grade", "grade_factor")}
        ic = existing or IncentiveCalculation(employee_id=employee_id, period=period)
        ic.contract_id = calc_data.get("contract_id")
        ic.net_revenue = calc_data.get("net_revenue", 0)
        ic.amount = calc_data.get("amount", 0)
        ic.status = "DRAFT"
        ic.snapshot = snap
        ic.created_by = actor
        if existing is None:
            session.add(ic)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="hr.incentive_calculations", target_id=str(ic.id),
                                  after={"amount": float(ic.amount), "period": period})
        return ic

    async def review(self, session: AsyncSession, *, actor: int, calc_id: int
                     ) -> IncentiveCalculation:
        ic = await self._get(session, calc_id)
        if ic.status != "DRAFT":
            raise ValidationError(f"검토 가능 상태가 아닙니다: {ic.status}")
        ic.status, ic.reviewed_by, ic.reviewed_at = "REVIEWED", actor, datetime.now(timezone.utc)
        return ic

    async def approve(self, session: AsyncSession, *, actor: int, calc_id: int
                      ) -> IncentiveCalculation:
        """승인 — PayrollLine(INCENTIVE) 반영 + 미지급 전표(급여비용/미지급금). 검토자≠승인자 권장."""
        ic = await self._get(session, calc_id, lock=True)   # 동시 승인 경합 방지(중복 라인)
        if ic.status != "REVIEWED":
            raise ValidationError(f"승인 가능 상태가 아닙니다(검토 선행): {ic.status}")
        amount = int(ic.amount)
        if amount > 0:
            # 성과급 원천징수 — 기관 설정(NONE 기본/FLAT 정률/COMBINED 상여합산·H1). 세무 정책이라
            # 하드코딩 안 함(관리화면 설정).
            from erp.hr import statutory
            from erp.hr.payroll_settings import PayrollSettingsService
            cfg = await PayrollSettingsService().effective(session)
            method = cfg["incentive_withholding_method"]
            withheld, wh_formula, local_wh = 0, None, 0
            if method == "FLAT" and cfg["incentive_withholding_rate"] > 0:
                rate = cfg["incentive_withholding_rate"]
                withheld = int(round(amount * rate))
                wh_formula = f"성과급 {amount:,} × {rate * 100:g}%"
            elif method == "COMBINED":
                monthly, prepaid, tax_basis = await self._monthly_regular(session, ic.employee_id, ic.period)
                # 부양가족 수 전달(계산검토 M-10) — 미전달(기본1) 시 A(재계산)와 C(실납부·실제
                # 부양 기준)의 기준이 어긋나 부양가족 많은 직원의 상여 원천징수가 과다.
                from erp.hr import withholding_table as wht
                # A(월급+상여)는 C(그 달 실제 기록된 TAX)와 **같은 근거**로 — 표로 계산된 줄이면 그 표·가족·자녀, 산식 줄
                # (활성화 전 생성분·옛 '간이세액표' 표기 포함)이면 산식. 섞이면 과다(A 산식·C 표) 또는 과소(A 표·C 산식)
                # 징수가 된다(v1.297 검토 2회). TAX 줄이 없을 때만 현재 활성 표 기준.
                if tax_basis is not None and tax_basis.get("table_id"):
                    wtable = await wht.get_table(session, int(tax_basis["table_id"]))
                    fam = int(tax_basis.get("dependents") or 1)
                    kids = int(tax_basis.get("children") or 0)
                elif tax_basis is not None:
                    wtable, kids = None, 0
                    fam = int(tax_basis.get("dependents") or 1)
                else:
                    wtable, fam, kids = await wht.employee_basis(
                        session, ic.employee_id, on=date(int(ic.period[:4]), int(ic.period[5:7]), 1))
                t = statutory.bonus_income_tax(monthly, amount, months=cfg["combined_months"],
                                               year=int(ic.period[:4]), dependents=fam,
                                               prepaid_monthly_tax=prepaid, table=wtable, children=kids)
                # 2026-10-09 전체 흐름 검토 C — 소득세와 지방소득세를 별도 라인으로(INCTAX/INCLOCALTAX).
                # 한 줄에 합치면 원천징수이행상황 '소득세'에 지방세가 섞여 과대신고(지방세는 별도 신고).
                local_wh = int(t["지방소득세"])
                withheld = int(t["소득세"]) + local_wh
                wh_formula = f"상여세액(시행령§194·{cfg['combined_months']}개월·기납부차감)"
            withheld = max(0, min(withheld, amount))    # 원천징수는 성과급을 초과할 수 없음(음수 CR 방지)
            local_wh = min(local_wh, withheld) if method == "COMBINED" else 0
            await self._add_payroll_line(session, ic.employee_id, ic.period, amount, ic.snapshot,
                                         withheld - local_wh, wh_formula, local_withheld=local_wh)
            # 전기 시퀀스 — 취소(역분개)→재승인 시 신규 source_ref로 정상 전기(멱등 충돌 방지)
            ic.posting_seq = (ic.posting_seq or 0) + 1
            ic.voucher_ref = f"incentive-{ic.id}-{ic.posting_seq}"
            from erp.hr.cost_center import employee_cost_center
            cc = await employee_cost_center(session, ic.employee_id)   # G9 부서별 인건비
            gl = [VoucherLineDTO(ACC_SALARY_EXP, "DEBIT", amount, cost_center=cc)]
            if withheld > 0:                            # DR 급여비용 / CR 예수금(원천징수) / CR 미지급
                gl.append(VoucherLineDTO(ACC_WITHHOLDING, "CREDIT", withheld))
            gl.append(VoucherLineDTO(ACC_AP, "CREDIT", amount - withheld))
            await _vouchers.create(
                session, source_module="hr.incentive", source_ref=ic.voucher_ref,
                voucher_on=_period_end(ic.period), created_by=actor,
                memo=f"성과급 {ic.period} (직원 {ic.employee_id})", lines=gl)
        ic.status, ic.approved_by, ic.approved_at = "APPROVED", actor, datetime.now(timezone.utc)
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="hr.incentive_calculations", target_id=str(ic.id),
                                  after={"status": "APPROVED", "amount": amount})
        return ic

    async def cancel(self, session: AsyncSession, *, actor: int, calc_id: int,
                     reason: str | None = None) -> IncentiveCalculation:
        """취소/claw-back — 역분개 전표 + INCENTIVE 라인 제거(지급 전). 지급 후는 차기 상계 정책(후속)."""
        ic = await self._get(session, calc_id, lock=True)
        if ic.status not in ("APPROVED", "REVIEWED", "DRAFT"):
            raise ValidationError(f"취소 불가 상태: {ic.status}")
        if ic.status == "APPROVED" and ic.voucher_ref:
            await self._remove_payroll_line(session, ic.employee_id, ic.period)
            # 역분개 — 실제 전기된 전표를 그대로 반대 분개(원천징수 2150 포함 정확 상쇄).
            # 하드코딩 차/대(급여비용/미지급 전액)는 원천징수 존재 시 2150·2110에 유령잔액을 남김.
            from erp.finance.models import Voucher
            v = await session.scalar(select(Voucher).where(
                Voucher.source_module == "hr.incentive", Voucher.source_ref == ic.voucher_ref))
            if v is not None and v.status != "REVERSED":
                await _vouchers.reverse(session, voucher_id=v.id,
                                        reason=f"성과급 취소({reason or '사유없음'})", created_by=actor)
        ic.status, ic.note = "CANCELED", reason
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="hr.incentive_calculations", target_id=str(ic.id),
                                  after={"status": "CANCELED", "reason": reason})
        return ic

    async def dashboard(self, session: AsyncSession, *, period: str) -> dict:
        """성과급 대시보드 — 의료진(ACTIVE 계약)별 실적·미리보기·산정 상태 결합."""
        pairs = (await session.execute(
            select(MedicalStaffContract, Employee)
            .join(Employee, Employee.id == MedicalStaffContract.employee_id)
            .where(MedicalStaffContract.status == "ACTIVE")
            .order_by(Employee.name))).all()
        rev = await _revenue.aggregate(session, period=period)
        rev_map = {r["doctor_id"]: r for r in rev["rows"]}
        calcs = {c.employee_id: c for c in await self.list_period(session, period=period)}
        rows, preview_total, approved_total = [], 0, 0
        for contract, emp in pairs:
            r = rev_map.get(emp.id)
            net = r["net_revenue"] if r else 0.0
            rate = float(contract.incentive_rate) if contract.incentive_rate is not None else None
            preview = calculate(incentive_type=contract.incentive_type, incentive_rate=rate,
                                params=contract.incentive_params, net_revenue=net)
            calc = calcs.get(emp.id)
            preview_total += preview["amount"]
            if calc and calc.status == "APPROVED":
                approved_total += int(calc.amount)
            rows.append({
                "employee_id": emp.id, "name": emp.name, "job_category": emp.job_category,
                "net_revenue": net, "basis": r["basis"] if r else None,
                "incentive_type": contract.incentive_type, "rate_pct": rate,
                "preview_amount": preview["amount"],
                "calc_id": calc.id if calc else None,
                "calc_status": calc.status if calc else None,
                "calc_amount": float(calc.amount) if calc else None})
        return {"period": period, "rows": rows,
                "totals": {"preview": preview_total, "approved": approved_total,
                           "doctors": len(rows)}}

    async def list_period(self, session: AsyncSession, *, period: str
                          ) -> list[IncentiveCalculation]:
        return list((await session.execute(
            select(IncentiveCalculation).where(IncentiveCalculation.period == period)
            .order_by(IncentiveCalculation.id))).scalars())

    async def _get(self, session, calc_id, *, lock: bool = False) -> IncentiveCalculation:
        if lock:   # 승인/취소 — 동시 처리 경합(중복 라인·이중 전기) 방지 행잠금
            ic = await session.scalar(
                select(IncentiveCalculation).where(IncentiveCalculation.id == calc_id)
                .with_for_update())
        else:
            ic = await session.get(IncentiveCalculation, calc_id)
        if ic is None:
            raise ValidationError("산정 내역이 없습니다")
        return ic

    async def _payroll_record(self, session, employee_id, period) -> PayrollRecord:
        rec = await session.scalar(select(PayrollRecord).where(
            PayrollRecord.employee_id == employee_id, PayrollRecord.yyyymm == period))
        if rec is None:
            rec = PayrollRecord(employee_id=employee_id, yyyymm=period)
            session.add(rec)
            await session.flush()
        return rec

    async def _monthly_regular(self, session, employee_id, period) -> tuple[float, float, dict | None]:
        """당월 정규 급여(성과급 제외)와 기납부 소득세(TAX 라인) — COMBINED 상여세액의 합산 기준.

        기납부 소득세는 시행령 §194의 C(지급대상기간 기납부세액) 차감용(정식). 급여 미확정이면 (0,0)."""
        from sqlalchemy import func

        from erp.hr.models import PayrollRecord
        rec = await session.scalar(select(PayrollRecord).where(
            PayrollRecord.employee_id == employee_id, PayrollRecord.yyyymm == period))
        if rec is None:
            return 0.0, 0.0, None
        gross = float(await session.scalar(select(func.coalesce(func.sum(PayrollLine.amount), 0)).where(
            PayrollLine.record_id == rec.id, PayrollLine.item_kind == "EARNING",
            PayrollLine.item_code.notin_(_EXTERNAL_CODES))) or 0)
        tax_line = await session.scalar(select(PayrollLine).where(
            PayrollLine.record_id == rec.id, PayrollLine.item_code == "TAX"))
        tax = float(tax_line.amount) if tax_line is not None else 0.0
        return gross, tax, (dict(tax_line.calc_basis or {}) if tax_line is not None else None)

    async def _add_payroll_line(self, session, employee_id, period, amount, snapshot=None,
                                withheld=0, wh_formula=None, local_withheld=0):
        rec = await self._payroll_record(session, employee_id, period)
        await session.execute(delete(PayrollLine).where(
            PayrollLine.record_id == rec.id,
            PayrollLine.item_code.in_(_EXTERNAL_CODES)))
        basis = None
        if snapshot:   # 성과급 계산근거 — 실적·산식(명세서 표기·P1/P2)
            net, rate = snapshot.get("net_revenue"), snapshot.get("rate_pct")
            basis = {"incentive_type": snapshot.get("incentive_type"),
                     "rate_pct": rate, "net_revenue": net,
                     "formula": (f"순수입 {int(net):,} × {rate:g}%"
                                 if net is not None and rate is not None else None)}
        session.add(PayrollLine(record_id=rec.id, item_code="INCENTIVE",
                                item_kind="EARNING", amount=amount, calc_basis=basis))
        if withheld > 0:   # 성과급 원천징수(FLAT/COMBINED) — 명세 공제 라인·GL 예수금과 정합
            session.add(PayrollLine(
                record_id=rec.id, item_code="INCTAX", item_kind="DEDUCTION", amount=withheld,
                calc_basis={"formula": wh_formula, "base": int(amount)}))
        if local_withheld > 0:   # 성과급 지방소득세(COMBINED) — 소득세와 분리(2026-10-09 검토 C)
            session.add(PayrollLine(
                record_id=rec.id, item_code="INCLOCALTAX", item_kind="DEDUCTION",
                amount=local_withheld, calc_basis={"formula": "성과급 소득세 × 10%", "rate": 0.1}))
        await session.flush()

    async def _remove_payroll_line(self, session, employee_id, period):
        rec = await session.scalar(select(PayrollRecord).where(
            PayrollRecord.employee_id == employee_id, PayrollRecord.yyyymm == period))
        if rec is not None:
            await session.execute(delete(PayrollLine).where(
                PayrollLine.record_id == rec.id,
                PayrollLine.item_code.in_(_EXTERNAL_CODES)))
