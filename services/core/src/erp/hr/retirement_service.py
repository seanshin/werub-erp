"""퇴직급여충당부채 — 월별 충당 적립. 전 직원 퇴직금추계액 증가분을 비용·부채로 전기.

추계액 = statutory.severance(평균임금, 입사일, 기말). 당월 전입액 = 추계액 − 직원 누적충당.
전입 합계 → 퇴직급여비용(5320)/퇴직급여충당부채(2120) 전표. retire:{period} 멱등.
"""
from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.months import require_ym
from erp.hr import statutory
from erp.hr.models import (
    ActuarialAssumption, Employee, PayrollLine, PayrollRecord, RetirementProvision,
)

ACC_PROVISION = "2120"   # 퇴직급여충당부채 (DB형)
ACC_EXPENSE = "5320"     # 퇴직급여비용
ACC_DC_PAYABLE = "2110"  # 미지급금 (DC형 부담금 — 연금사업자 예치 예정)
ACC_ACTUARIAL_OCI = "3410"  # 보험수리적손익 (기타포괄손익, K-IFRS 1019)


async def _avg_wage(session: AsyncSession, employee_id: int, period: str) -> float:
    """평균임금(월 단위 근사) — 최근(기준월 이하) **3개월** 급여 EARNING 합 ÷ 개월수.

    근기법 평균임금(3개월 임금총액 기준·계산검토 M-13 — 종전 1개월은 상여·변동수당 월에
    과대/과소). 이력이 3개월 미만이면 있는 개월수로 평균(신규 입사 대응).
    """
    recents = (await session.scalars(
        select(PayrollRecord.id).where(PayrollRecord.employee_id == employee_id,
                                       PayrollRecord.yyyymm <= period)
        .order_by(PayrollRecord.yyyymm.desc()).limit(3))).all()
    if not recents:
        return 0.0
    total = float(await session.scalar(
        select(func.coalesce(func.sum(
            case((PayrollLine.item_kind == "EARNING", PayrollLine.amount), else_=0)), 0))
        .where(PayrollLine.record_id.in_(list(recents)))) or 0)
    return total / len(recents)


def _months_back(d: date, n: int) -> date:
    """d에서 n개월 전 같은 날(월말 클램프)."""
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    m += 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


async def statutory_avg_wage(session: AsyncSession, emp: Employee, *, anchor: date) -> float:
    """근기법 §2 평균임금(월 환산) — 기준일 이전 **3개월 임금총액 ÷ 그 기간 총일수 × 30**.

    2026-10-09 시나리오 S-C: `_avg_wage`(최근 3개 급여 record의 EARNING 합 ÷ record 수)는 월중 퇴사·입사로
    **일할된 달을 한 달로 셌다** — 9/20 퇴사(기본급 360만, 9월 20/30 일할 240만)의 평균임금이 240만으로
    잡혀 퇴직금·연차수당이 약 1/3 과소(법정 체불). 여기서는 달마다 그 달 임금을 **그 달 재직일**로 나눠
    일급을 구하고, 산정기간과 겹치는 날만큼 더한 뒤 산정기간 일수로 나눈다.

    - anchor = 산정 마지막 날(퇴사일=최종 근무일·재직자는 기준일). 급여가 아직 없는 달(예: 당월 미생성)은
      산정기간에서 빼려고 기간 끝을 **급여가 있는 마지막 달 말일**로 당긴다.
    - 입사 3개월 미만이면 입사일부터. 급여 record가 없는 달은 분자·분모 모두에서 뺀다. 이력이 없으면 0(호출부 폴백).
    """
    last = await session.scalar(
        select(func.max(PayrollRecord.yyyymm)).where(
            PayrollRecord.employee_id == emp.id,
            PayrollRecord.yyyymm <= f"{anchor.year:04d}-{anchor.month:02d}"))
    if last is None:
        return 0.0
    ly, lm = int(last[:4]), int(last[5:7])
    end = min(anchor, date(ly, lm, monthrange(ly, lm)[1]))
    # 기간 = (end 다음날)의 3개월 전 같은 날부터 — 말일 끝(11/30)이면 9/1부터. `_months_back(end,3)+1일`은
    # 월말 클램프 때문에 8/31부터가 돼 앞달 하루가 섞였다(2월 말일은 이틀·시나리오 2회차 검토)
    start = _months_back(end + timedelta(days=1), 3)
    if emp.hired_on and emp.hired_on > start:
        start = emp.hired_on
    if start > end:
        return 0.0
    keys, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        keys.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    rows = (await session.execute(
        select(PayrollRecord.yyyymm, func.coalesce(func.sum(
            case((PayrollLine.item_kind == "EARNING", PayrollLine.amount), else_=0)), 0))
        .join(PayrollLine, PayrollLine.record_id == PayrollRecord.id)
        .where(PayrollRecord.employee_id == emp.id, PayrollRecord.yyyymm.in_(keys))
        .group_by(PayrollRecord.yyyymm))).all()
    wages, covered = 0.0, 0
    for ym, earn in rows:
        y, m = int(ym[:4]), int(ym[5:7])
        ms, me = date(y, m, 1), date(y, m, monthrange(y, m)[1])
        es = max(ms, emp.hired_on) if emp.hired_on else ms        # 그 달 재직 구간(일할 기준과 동일)
        ee = min(me, emp.resigned_on) if emp.resigned_on else me
        employed = (ee - es).days + 1
        overlap = (min(ee, end) - max(es, start)).days + 1
        if employed > 0 and overlap > 0:
            wages += float(earn or 0) * overlap / employed
            covered += overlap
    # 분모는 **급여 record가 있는 달의 겹친 날**만 — ERP 도입 전·이관 전처럼 record 자체가 없는 달을
    # 임금 0으로 세면 평균이 희석된다(무급 휴직 등 실제 저임금 달은 record가 있으므로 그대로 반영).
    return wages / covered * 30 if covered else 0.0


class RetirementService:
    @staticmethod
    async def _avg_wages_bulk(session: AsyncSession, *, period: str) -> dict[int, float]:
        """전 직원 평균임금(최근 **3개월** EARNING 평균·M-13)을 일괄 산정 — N+1 제거.

        직원별 최근 3개 record(정렬 후 앞 3행 채택 — sqlite/PG 공통) → EARNING 합 ÷ 개월수.
        급여 record 없는 직원은 결과 미포함(호출부 .get(.,0.0)) — _avg_wage와 동일 의미.
        """
        latest = (await session.execute(
            select(PayrollRecord.employee_id, PayrollRecord.id)
            .where(PayrollRecord.yyyymm <= period)
            .order_by(PayrollRecord.employee_id, PayrollRecord.yyyymm.desc()))).all()
        rec_to_emp: dict[int, int] = {}
        months_by_emp: dict[int, int] = {}
        for emp_id, rec_id in latest:           # 직원별 앞 3행 = 최근 3개월
            if months_by_emp.get(emp_id, 0) < 3:
                months_by_emp[emp_id] = months_by_emp.get(emp_id, 0) + 1
                rec_to_emp[rec_id] = emp_id
        if not rec_to_emp:
            return {}
        sums = (await session.execute(
            select(PayrollLine.record_id, func.coalesce(func.sum(
                case((PayrollLine.item_kind == "EARNING", PayrollLine.amount), else_=0)), 0))
            .where(PayrollLine.record_id.in_(list(rec_to_emp)))
            .group_by(PayrollLine.record_id))).all()
        total_by_emp: dict[int, float] = {}
        for rid, total in sums:
            emp_id = rec_to_emp[rid]
            total_by_emp[emp_id] = total_by_emp.get(emp_id, 0.0) + float(total or 0)
        return {emp_id: t / months_by_emp[emp_id] for emp_id, t in total_by_emp.items()}

    async def accrue_monthly(self, session: AsyncSession, *, period: str,
                             user_id: int | None = None) -> dict:
        """월별 퇴직급여충당부채 적립 — 전 직원 전입액 산정 후 전표 1건."""
        period = require_ym(period)
        from erp.finance.service import VoucherLineDTO, VoucherService
        y, m = int(period[:4]), int(period[5:7])
        as_of = date(y, m, monthrange(y, m)[1])
        # 재적 직원(입사 ≤ 기말, 미퇴사 또는 기말 이후 퇴사)
        emps = (await session.execute(select(Employee).where(
            Employee.hired_on <= as_of,
            (Employee.resigned_on.is_(None)) | (Employee.resigned_on >= as_of)))).scalars().all()
        # ── N+1 제거: 평균임금·이전누적·기존충당을 일괄 사전로드 ──
        avg_by_emp = await self._avg_wages_bulk(session, period=period)
        prev_rows = (await session.execute(
            select(RetirementProvision.employee_id, RetirementProvision.cumulative)
            .where(RetirementProvision.period < period)
            .order_by(RetirementProvision.employee_id, RetirementProvision.period.desc()))).all()
        prev_by_emp: dict[int, float] = {}
        for emp_id, cum in prev_rows:           # 직원별 첫 행 = 직전 기간 누적
            if emp_id not in prev_by_emp:
                prev_by_emp[emp_id] = float(cum or 0)
        existing_by_emp = {r.employee_id: r for r in (await session.execute(
            select(RetirementProvision).where(
                RetirementProvision.period == period))).scalars().all()}
        total_db = total_dc = 0.0
        count = 0
        for emp in emps:
            avg = avg_by_emp.get(emp.id, 0.0)
            prev = prev_by_emp.get(emp.id, 0.0)
            if (emp.pension_type or "DB") == "DC":   # DC형 — 부담금(연간임금/12 ≈ avg/12) 비용·미지급
                inc = round(avg / 12)
                est = cumulative = prev + inc
                total_dc += inc
            else:                                    # DB형 — 추계액 증가분 충당
                est = cumulative = statutory.severance(avg, hire_date=emp.hired_on, as_of=as_of)["퇴직금"]
                inc = est - prev
                total_db += inc
            existing = existing_by_emp.get(emp.id)
            if existing is not None:                 # 멱등 — 재적립 시 갱신
                existing.avg_wage, existing.estimate = avg, est
                existing.increment, existing.cumulative = inc, cumulative
            else:
                session.add(RetirementProvision(employee_id=emp.id, period=period, avg_wage=avg,
                                                estimate=est, increment=inc, cumulative=cumulative,
                                                voucher_ref=f"retire:{period}"))
            count += 1
        db, dc = round(total_db), round(total_dc)
        voucher_id = None
        lines: list = []
        if db > 0:        # DB 충당 전입 (퇴직급여비용/퇴직급여충당부채)
            lines += [VoucherLineDTO(ACC_EXPENSE, "DEBIT", db),
                      VoucherLineDTO(ACC_PROVISION, "CREDIT", db)]
        elif db < 0:
            lines += [VoucherLineDTO(ACC_PROVISION, "DEBIT", -db),
                      VoucherLineDTO(ACC_EXPENSE, "CREDIT", -db)]
        if dc > 0:        # DC 부담금 (퇴직급여비용/미지급금 — 연금사업자 예치)
            lines += [VoucherLineDTO(ACC_EXPENSE, "DEBIT", dc),
                      VoucherLineDTO(ACC_DC_PAYABLE, "CREDIT", dc)]
        ti = db + dc
        if lines:
            v = await VoucherService().create(
                session, source_module="hr.retirement", source_ref=f"retire:{period}",
                voucher_on=as_of, lines=lines, created_by=user_id,
                memo=f"{period} 퇴직급여 적립(DB충당 {db:,}·DC부담금 {dc:,}, 직원 {count})")
            voucher_id = v.id
        return {"period": period, "employees": count, "total_increment": ti,
                "db_provision": db, "dc_contribution": dc, "voucher_id": voucher_id}

    async def provisions(self, session: AsyncSession, *, period: str) -> dict:
        rows = (await session.execute(select(RetirementProvision).where(
            RetirementProvision.period == period)
            .order_by(RetirementProvision.cumulative.desc()))).scalars().all()
        # 표 첫 열이 원시 PK라 누구의 충당인지 식별할 수 없었다
        from erp.hr.employee_service import employee_labels
        labels = await employee_labels(session, [r.employee_id for r in rows])
        return {"period": period,
                "total_estimate": sum(float(r.estimate) for r in rows),
                "total_increment": sum(float(r.increment) for r in rows),
                "items": [{"employee_id": r.employee_id,
                           "employee_name": labels.get(r.employee_id, {}).get("name"),
                           "employee_no": labels.get(r.employee_id, {}).get("employee_no"),
                           "avg_wage": float(r.avg_wage),
                           "estimate": float(r.estimate), "increment": float(r.increment),
                           "cumulative": float(r.cumulative)} for r in rows]}

    # ── 보험수리적 DBO 평가·재측정(OCI) — K-IFRS 1019 ──────────────────
    async def _assumption(self, session: AsyncSession, *, as_of: date):
        a = (await session.execute(
            select(ActuarialAssumption).where(ActuarialAssumption.valid_from <= as_of)
            .order_by(ActuarialAssumption.valid_from.desc()).limit(1))).scalar_one_or_none()
        if a is None:
            return 0.04, 0.03, 60      # 기본 가정(할인 4%·임금상승 3%·정년 60)
        return float(a.discount_rate), float(a.salary_growth), int(a.retirement_age)

    async def _provision_balance(self, session: AsyncSession, *, as_of: date) -> float:
        from erp.finance.models import Voucher, VoucherLine
        d, cr = (await session.execute(
            select(
                func.coalesce(func.sum(case(
                    (VoucherLine.side == "DEBIT", VoucherLine.amount), else_=0)), 0),
                func.coalesce(func.sum(case(
                    (VoucherLine.side == "CREDIT", VoucherLine.amount), else_=0)), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code == ACC_PROVISION, Voucher.voucher_on <= as_of))).one()
        return float(cr) - float(d)    # 충당부채=대변잔액

    async def dbo_preview(self, session: AsyncSession, *, period: str) -> dict:
        """확정급여채무(DBO) 보험수리 평가 — 명목추계×((1+임금상승)/(1+할인율))^잔여근속.

        DB형 재적 직원 합산. **재측정(OCI) = DBO − 명목추계 합**(보험수리 가정차이만 OCI로 분리).
        명목추계(정상근무원가)는 accrue_monthly가 P/L로 적립 → 월적립 미실행과 무관하게 OCI 과대 방지.
        정년까지 잔여연수는 생년월일 기반(없으면 보험수리 조정 제외=factor 1).
        """
        y, m = int(period[:4]), int(period[5:7])
        as_of = date(y, m, monthrange(y, m)[1])
        d_rate, g_rate, ret_age = await self._assumption(session, as_of=as_of)
        emps = (await session.execute(select(Employee).where(
            Employee.hired_on <= as_of,
            (Employee.resigned_on.is_(None)) | (Employee.resigned_on >= as_of)))).scalars().all()
        dbo_total = nominal_total = 0.0
        no_birth = 0
        for emp in emps:
            if (emp.pension_type or "DB") == "DC":
                continue
            avg = await _avg_wage(session, emp.id, period)
            nominal = statutory.severance(avg, hire_date=emp.hired_on, as_of=as_of)["퇴직금"]
            if not nominal:
                continue
            nominal_total += nominal
            if emp.birth_date is None:
                no_birth += 1
                factor = 1.0           # 잔여근속 불명 → 보험수리 조정 없음(명목 유지)
            else:
                age = (as_of - emp.birth_date).days / 365.25
                n = max(0.0, ret_age - age)
                factor = ((1 + g_rate) / (1 + d_rate)) ** n
            dbo_total += nominal * factor
        dbo_total, nominal_total = round(dbo_total), round(nominal_total)
        current = round(await self._provision_balance(session, as_of=as_of))
        return {"period": period, "as_of": str(as_of), "discount_rate": d_rate,
                "salary_growth": g_rate, "retirement_age": ret_age, "dbo": dbo_total,
                "nominal_estimate": nominal_total, "provision_balance": current,
                "remeasurement": dbo_total - nominal_total,   # 보험수리 가정차이(OCI)만
                "employees_no_birth": no_birth}

    async def dbo_post(self, session: AsyncSession, *, period: str,
                       user_id: int | None = None) -> dict:
        """DBO 재측정 차액을 OCI(3410)/충당부채(2120)로 전기(멱등). 보험수리적손익=OCI."""
        from erp.finance.models import Voucher
        from erp.finance.service import VoucherLineDTO, VoucherService
        sref = f"dbo-remeasure:{period}"
        if await session.scalar(select(Voucher.id).where(
                Voucher.source_module == "closing-ifrs", Voucher.source_ref == sref)):
            return {"posted": False, "reason": "이미 전기(동일 기간)", **(await self.dbo_preview(session, period=period))}
        p = await self.dbo_preview(session, period=period)
        delta = p["remeasurement"]
        if delta == 0:
            return {"posted": False, "reason": "조정 없음", **p}
        y, m = int(period[:4]), int(period[5:7])
        as_of = date(y, m, monthrange(y, m)[1])
        if delta > 0:        # DBO > 명목 → 충당 증가(보험수리적손실 OCI)
            lines = [VoucherLineDTO(ACC_ACTUARIAL_OCI, "DEBIT", delta),
                     VoucherLineDTO(ACC_PROVISION, "CREDIT", delta)]
        else:                # DBO < 명목 → 충당 감소(보험수리적이익 OCI)
            lines = [VoucherLineDTO(ACC_PROVISION, "DEBIT", -delta),
                     VoucherLineDTO(ACC_ACTUARIAL_OCI, "CREDIT", -delta)]
        v = await VoucherService().create(
            session, source_module="closing-ifrs", source_ref=sref,
            voucher_on=as_of, lines=lines,
            memo="퇴직급여 보험수리적 재측정(OCI, K-IFRS 1019)", created_by=user_id,
            allow_closed=True)
        return {"posted": True, "voucher_id": v.id, **p}
