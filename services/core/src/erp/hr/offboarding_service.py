"""퇴직 정산(오프보딩) — 인사 P2-①.

퇴사 처리(resigned_on) 후 정산 절차를 한 번에:
① 연차 미사용 수당 = 잔여일수 × 통상일급(기본급/209h × 8h)
② 퇴직금 = 평균임금(최근 확정급여 EARNING, 없으면 보수 프로필) × 근속연수 (statutory)
③ 전표: 충당부채(2120) 상계 + 부족분 퇴직급여비용(5320) + 연차수당 급여비용(5310)
   / 대변 예수금(2150, 원천징수) + 미지급금(2110, 실지급). 직원당 1회 멱등(UNIQUE + voucher source_ref).
④ 원천징수(2026-10-09 전체 흐름 검토 C) — 종전엔 퇴직금·연차수당 전액을 2110에 대기해 세금 없이
   지급했다(과소징수 → 원천징수의무자 가산세).
   - 퇴직금 → 퇴직소득세(statutory.retirement_income_tax·근속연수공제/환산급여 방식) + 지방소득세 10%.
   - 연차 미사용 수당 → **근로소득**(퇴직소득 아님 — 퇴직 전 근로 대가)이라 상여 방식 원천징수
     (statutory.bonus_income_tax: 평균임금 월급 + 수당, 부양가족은 보수 프로필) + 지방소득세.
     급여 라인으로 올리는 대신 정산 전표 안에서 원천징수 — 급여 확정 전표와 이중 전기를 피하는 최소
     변경(급여월 레코드가 이미 확정·잠금일 수 있음). ⚠연말정산 총급여 집계(급여 라인 기준)에는 이
     수당이 빠지므로 퇴사자 중도 정산 시 수기 합산 필요 — 후속 과제.
4대보험 상실 신고는 퇴사 처리 시 자동 적재(P1-③) — 본 절차와 독립.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.hr import statutory
from erp.hr.leave_service import LeaveService
from erp.hr.models import (
    CompensationProfile, Employee, OffboardingSettlement, RetirementProvision,
)
from erp.hr.payroll_rules import STANDARD_MONTHLY_HOURS
from erp.hr.retirement_service import statutory_avg_wage

ACC_PROVISION, ACC_SEVERANCE_EXP = "2120", "5320"
ACC_SALARY_EXP, ACC_PAYABLE = "5310", "2110"
ACC_WITHHOLDING = "2150"   # 예수금(원천징수)

_audit = AuditService()
_leave = LeaveService()
_vouchers = VoucherService()


def _scaled(pv: dict, key: str, total: float) -> int:
    w = pv["withholding"]
    full = float(w["total"] or 0)
    v = float(w[key] or 0)
    if full <= 0 or full <= total:
        return int(v)
    return int(v * total / full)


class OffboardingService:
    async def preview(self, session: AsyncSession, *, employee_id: int) -> dict:
        emp = await session.get(Employee, employee_id)
        if emp is None:
            raise ValidationError("존재하지 않는 직원입니다")
        if emp.resigned_on is None:
            raise ValidationError("퇴사 처리(resigned_on)가 선행되어야 합니다")

        avg = await statutory_avg_wage(session, emp, anchor=emp.resigned_on)
        if avg <= 0:        # 확정 급여 이력 없음 → 보수 프로필 기본급
            profile = await session.scalar(select(CompensationProfile).where(
                CompensationProfile.employee_id == employee_id))
            avg = float(profile.base_pay) if profile else 0.0

        remaining = await _leave.remaining_days(session, employee_id=employee_id,
                                                year=emp.resigned_on.year)
        remaining = max(remaining, 0.0)
        daily_wage = avg / STANDARD_MONTHLY_HOURS * 8       # 통상일급
        leave_allowance = round(remaining * daily_wage)

        sev = statutory.severance(avg, hire_date=emp.hired_on, as_of=emp.resigned_on)
        # 원천징수 — 퇴직소득세(퇴직금)·근로소득세 상여방식(연차수당)·지방소득세 10% (검토 C)
        tax_year = emp.resigned_on.year
        retire_tax = {"소득세": 0, "지방소득세": 0, "합계": 0}
        if sev["퇴직금"] > 0:
            retire_tax = statutory.retirement_income_tax(
                sev["퇴직금"], year=tax_year,
                years=statutory.service_years(emp.hired_on, emp.resigned_on))
        leave_tax = {"소득세": 0, "지방소득세": 0}
        if leave_allowance > 0:
            from erp.hr import withholding_table as wht
            wtable, fam, kids = await wht.employee_basis(session, employee_id, on=emp.resigned_on.replace(day=1))
            leave_tax = statutory.bonus_income_tax(avg, leave_allowance, months=1, dependents=fam, year=tax_year,
                                                   table=wtable, children=kids)
        withholding = {
            "retirement_income_tax": int(retire_tax["소득세"]),
            "retirement_local_tax": int(retire_tax["지방소득세"]),
            "leave_income_tax": int(leave_tax["소득세"]),
            "leave_local_tax": int(leave_tax["지방소득세"]),
            "retirement_tax_detail": retire_tax,
        }
        withholding["total"] = (withholding["retirement_income_tax"]
                                + withholding["retirement_local_tax"]
                                + withholding["leave_income_tax"] + withholding["leave_local_tax"])
        provision = float(await session.scalar(
            select(RetirementProvision.cumulative)
            .where(RetirementProvision.employee_id == employee_id)
            .order_by(RetirementProvision.period.desc()).limit(1)) or 0)

        existing = await session.scalar(select(OffboardingSettlement).where(
            OffboardingSettlement.employee_id == employee_id))
        return {"employee_id": employee_id, "employee_no": emp.employee_no,
                "name": emp.name, "resigned_on": emp.resigned_on,
                "avg_monthly_wage": round(avg),
                "leave_remaining_days": remaining, "leave_allowance": leave_allowance,
                "severance": sev, "provision_cumulative": provision,
                "already_settled": existing is not None,
                "withholding": withholding,
                "total_payout": leave_allowance + sev["퇴직금"],          # 세전
                "net_payout": leave_allowance + sev["퇴직금"] - withholding["total"]}

    async def settle(self, session: AsyncSession, *, actor: int, employee_id: int) -> dict:
        pv = await self.preview(session, employee_id=employee_id)
        if pv["already_settled"]:
            raise ValidationError("이미 정산 완료된 직원입니다")
        severance = float(pv["severance"]["퇴직금"])
        allowance = float(pv["leave_allowance"])
        offset = min(pv["provision_cumulative"], severance)   # 충당부채 상계분

        voucher_ref = f"offboard:{employee_id}"
        voucher_id = None
        total = severance + allowance
        if total > 0:
            from erp.hr.cost_center import employee_cost_center
            cc = await employee_cost_center(session, employee_id)   # G9 부서별 인건비
            lines = []
            if offset > 0:
                lines.append(VoucherLineDTO(account_code=ACC_PROVISION, side="DEBIT",
                                            amount=offset, detail="퇴직급여충당부채 상계"))
            if severance - offset > 0:
                lines.append(VoucherLineDTO(account_code=ACC_SEVERANCE_EXP, side="DEBIT",
                                            amount=severance - offset, cost_center=cc,
                                            detail="퇴직급여비용(충당 부족분)"))
            if allowance > 0:
                lines.append(VoucherLineDTO(account_code=ACC_SALARY_EXP, side="DEBIT",
                                            amount=allowance, cost_center=cc,
                                            detail="연차 미사용 수당"))
            withheld = min(float(pv["withholding"]["total"]), total)
            if withheld > 0:   # 퇴직소득세·연차수당 근로소득세(+지방세) 예수 — 검토 C
                lines.append(VoucherLineDTO(account_code=ACC_WITHHOLDING, side="CREDIT",
                                            amount=withheld, detail="퇴직소득세·근로소득세 원천징수"))
            if total - withheld > 0:
                lines.append(VoucherLineDTO(account_code=ACC_PAYABLE, side="CREDIT",
                                            amount=total - withheld, detail="퇴직 정산 미지급금(실지급)"))
            v = await _vouchers.create(
                session, source_module="hr", source_ref=voucher_ref,
                voucher_on=pv["resigned_on"], lines=lines, created_by=actor,
                memo=f"퇴직 정산 — {pv['name']}({pv['employee_no']})")
            voucher_id = v.id

        session.add(OffboardingSettlement(
            employee_id=employee_id, resigned_on=pv["resigned_on"],
            leave_remaining_days=pv["leave_remaining_days"], leave_allowance=allowance,
            avg_monthly_wage=pv["avg_monthly_wage"], severance_amount=severance,
            provision_offset=offset, voucher_ref=voucher_ref if voucher_id else None,
            # 2026-10-09 전체 흐름 검토 2차 C: 원천징수 세액 보관 → 원천징수이행상황(A21)·영수증 반영
            # 실제 예수(2150) 전기액과 맞춘다 — 지급액 상한으로 깎였으면 비례 축소(3차 검토 C·LOW)
            retirement_income_tax=_scaled(pv, "retirement_income_tax", total),
            retirement_local_tax=_scaled(pv, "retirement_local_tax", total),
            leave_allowance_tax=_scaled(pv, "leave_income_tax", total),
            leave_allowance_local_tax=_scaled(pv, "leave_local_tax", total),
            settled_by=actor))
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="hr.offboarding_settlements",
                                  target_id=str(employee_id),
                                  after={"severance": severance, "allowance": allowance,
                                         "withheld": pv["withholding"]["total"]})
        return {**pv, "already_settled": True, "voucher_id": voucher_id,
                "provision_offset": offset}
