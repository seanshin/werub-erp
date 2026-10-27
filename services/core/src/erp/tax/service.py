"""세무 신고 집계 (FR-07 확장) — 원천징수이행상황·4대보험 공단신고용. 급여 데이터 기반.

읽기 전용 — 기존 급여(PayrollLine) + 법정계산(statutory) 조합. 신규 테이블 없음.
부가세: 의료용역은 원칙 면세(부가세법 §26①5) — 과세분(미용·일부 건강검진)은 별도 매출구분 필요.
"""
from sqlalchemy import String as SAString
from sqlalchemy import case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.months import require_ym
from erp.hr import statutory
from erp.hr.models import PayrollLine, PayrollRecord

_EARNING = func.coalesce(
    func.sum(case((PayrollLine.item_kind == "EARNING", PayrollLine.amount), else_=0)), 0)
_TAX = func.coalesce(   # TAX(근로소득세)+INCTAX(성과급 원천징수) — 성과급 지급액이 총지급액(EARNING)에
    # 포함되므로 그 원천징수세액도 함께 집계해야 예수금 GL·신고서가 정합(INCTAX 누락 시 과소신고).
    func.sum(case((PayrollLine.item_code.in_(("TAX", "INCTAX")), PayrollLine.amount), else_=0)), 0)
# 지방소득세(특별징수)는 지자체 별도 신고 — 소득세와 분리 집계(2026-10-09 전체 흐름 검토 C)
_LOCAL = func.coalesce(
    func.sum(case((PayrollLine.item_code.in_(("LOCALTAX", "INCLOCALTAX")), PayrollLine.amount),
                  else_=0)), 0)
# 급여 계산(build_payroll)이 보험료 기준에서 빼는 외부 라인 — hr.employee_service._EXTERNAL_LINE_CODES
_EXTERNAL_LINE_CODES = ("INCENTIVE", "INCTAX", "INCLOCALTAX")


class TaxService:
    async def withholding(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """원천징수이행상황 — 근로소득 인원·지급액·원천징수세액(간이세액표 기납부).

        2026-10-09 전체 흐름 검토 C:
        - **확정 급여만** 집계 — 미확정 초안(재생성 중·검토 전)이 신고 금액에 섞이던 결함.
        - 일용근로(DAILY)는 소득구분 **A03(일용근로소득)**으로 분리 — 종전엔 A01(간이세액)에 합산.
        - 지방소득세는 소득세와 분리(`지방소득세` 키) — 소득세 신고에 섞이지 않게.
        최상위 키(소득구분·인원·총지급액·원천징수세액)는 A01 값 — 화면·서식 하위호환.
        """
        from erp.hr.models import Employee
        is_daily = Employee.employment_type == "DAILY"
        rows = (await session.execute(
            select(is_daily, func.count(func.distinct(PayrollRecord.employee_id)),
                   _EARNING, _TAX, _LOCAL)
            .select_from(PayrollLine)
            .join(PayrollRecord, PayrollRecord.id == PayrollLine.record_id)
            .join(Employee, Employee.id == PayrollRecord.employee_id)
            .where(PayrollRecord.yyyymm == require_ym(yyyymm),
                   # 2026-10-09 전체 흐름 검토 2차 C: 성과급 외부 라인(INCENTIVE·INCTAX·INCLOCALTAX)은
                   # 성과급 승인 시점에 이미 2150 예수금으로 전기되므로 레코드 확정과 무관하게 집계한다.
                   # 그 달 정규 급여가 없는(=미확정 레코드에 외부 라인만 있는) 직원의 성과급 원천세가
                   # 신고에서 빠지던 결함. 정규 TAX/LOCALTAX 등은 종전대로 확정 급여만.
                   or_(PayrollRecord.confirmed_at.isnot(None),
                       PayrollLine.item_code.in_(_EXTERNAL_LINE_CODES)))
            .group_by(is_daily))).all()
        by = {bool(d): (int(c or 0), float(p or 0), float(t or 0), float(lt or 0))
              for d, c, p, t, lt in rows}

        def _part(code: str, key: bool) -> dict:
            c, p, t, lt = by.get(key, (0, 0.0, 0.0, 0.0))
            return {"소득구분": code, "인원": c, "총지급액": p, "원천징수세액": t, "지방소득세": lt}

        a01, a03 = _part("근로소득(A01)", False), _part("일용근로소득(A03)", True)
        a01_off, a21 = await self._offboarding_parts(session, yyyymm=yyyymm)
        parts = [a01, a03, a01_off, a21]
        return {"yyyymm": yyyymm, **a01, "구분": parts,
                "합계": {"인원": a01["인원"] + a03["인원"] + a21["인원"],
                       "총지급액": sum(p["총지급액"] for p in parts),
                       "원천징수세액": sum(p["원천징수세액"] for p in parts),
                       "지방소득세": sum(p["지방소득세"] for p in parts)},
                "주": "확정 급여 기준(성과급 외부 라인은 승인 시 예수 전기라 확정 무관). "
                      "퇴직 정산은 퇴사일 월 귀속 — 퇴직금=A21, 연차 미사용 수당=근로소득(A01 합산). "
                      "지방소득세는 지자체 특별징수 별도 신고."}

    async def _offboarding_parts(self, session: AsyncSession, *, yyyymm: str) -> tuple[dict, dict]:
        """퇴직 정산 원천징수 — 2026-10-09 전체 흐름 검토 2차 C.

        퇴직 정산(OffboardingSettlement)은 정산 전표에서 2150 예수금을 잡지만 급여 라인이 아니라
        원천징수이행상황 집계에서 통째로 빠졌다. 퇴직금·퇴직소득세는 A21(퇴직소득), 연차 미사용 수당과
        그 근로소득세(상여 방식)는 근로소득(A01 합산 대상)으로 별도 행. 귀속월=퇴사일(정산 전표일) 월.
        세액 컬럼(0253) 도입 전 정산분은 NULL → 0으로 집계(세액 미보관).
        """
        from datetime import date as _date

        from erp.hr.models import OffboardingSettlement as OS
        y, m = int(yyyymm[:4]), int(yyyymm[5:7])
        start = _date(y, m, 1)
        end = _date(y + (m == 12), (m % 12) + 1, 1)
        rows = (await session.execute(select(
            OS.severance_amount, OS.retirement_income_tax, OS.retirement_local_tax,
            OS.leave_allowance, OS.leave_allowance_tax, OS.leave_allowance_local_tax)
            .where(OS.resigned_on >= start, OS.resigned_on < end))).all()
        a21 = {"소득구분": "퇴직소득(A21)", "인원": 0, "총지급액": 0.0, "원천징수세액": 0.0,
               "지방소득세": 0.0}
        off = {"소득구분": "근로소득-퇴직정산 연차수당(A01 합산)", "인원": 0, "총지급액": 0.0,
               "원천징수세액": 0.0, "지방소득세": 0.0}
        for sev, rt, rlt, la, lt, llt in rows:
            if float(sev or 0) > 0:
                a21["인원"] += 1
                a21["총지급액"] += float(sev or 0)
                a21["원천징수세액"] += float(rt or 0)
                a21["지방소득세"] += float(rlt or 0)
            if float(la or 0) > 0:
                off["인원"] += 1
                off["총지급액"] += float(la or 0)
                off["원천징수세액"] += float(lt or 0)
                off["지방소득세"] += float(llt or 0)
        return off, a21

    async def insurance_filing(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """4대보험 공단신고 집계 — 고용형태 특례 반영(급여 생성과 동일 계산, 점검 M3).

        2026-10-09 전체 흐름 검토 C — 급여 계산(build_payroll)과 같은 입력으로 맞춤:
        ① 보수 = 정규 EARNING(성과급 등 외부 라인 제외 — 급여 공제도 성과급엔 보험료를 안 뗀다)
        ② 일용직은 근무일수(BASE 라인 calc_basis.days)를 넘겨 국민연금·건강 적용기준(월 8일)을 판정
           — 종전엔 일수 없이 계산해(=0일) 월 8일 이상 일한 일용직의 연금·건강이 신고에서 누락
        ③ 확정 급여만.
        """
        from erp.hr.models import Employee
        rows = (await session.execute(
            select(PayrollRecord.employee_id, Employee.employment_type, PayrollLine.item_code,
                   PayrollLine.amount, PayrollLine.calc_basis)
            .join(PayrollLine, PayrollLine.record_id == PayrollRecord.id)
            .join(Employee, Employee.id == PayrollRecord.employee_id)
            .where(PayrollRecord.yyyymm == require_ym(yyyymm), PayrollRecord.confirmed_at.isnot(None),
                   PayrollLine.item_kind == "EARNING",
                   PayrollLine.item_code.notin_(_EXTERNAL_LINE_CODES)))).all()
        from erp.hr.payroll_rules import load_nontax, nontax_exempt
        nontax = await load_nontax(session)
        per: dict[int, dict] = {}
        for emp_id, etype, code, amount, basis in rows:
            e = per.setdefault(emp_id, {"etype": etype or "REGULAR", "salary": 0.0, "days": None, "lines": []})
            e["salary"] += float(amount)
            e["lines"].append({"item_code": code, "amount": amount})
            if code == "BASE" and isinstance(basis, dict) and basis.get("days") is not None \
                    and e["etype"] == "DAILY":
                e["days"] = int(basis["days"])
        for e in per.values():   # 보수월액 = 비과세 제외(급여 공제 줄과 같은 기준·v1.303) — 직원 단위로 항목별 월 한도
            e["salary"] -= nontax_exempt(e.pop("lines"), nontax)
        year = int(yyyymm[:4])
        # 실제 원천공제된 금액(급여 공제 라인)이 신고의 원천 — 2026-10-10: 요율 정정 뒤 다시 계산하면 이미 공제·전기된
        # 금액과 신고가 경고 없이 갈렸다. 공제 라인이 없는 직원만 현재 요율로 계산하고 그 인원을 따로 밝힌다.
        _ded_key = {"NP": "국민연금", "HI": "건강보험", "LTC": "장기요양", "EI": "고용보험"}
        recorded = (await session.execute(
            select(PayrollRecord.employee_id, PayrollLine.item_code, PayrollLine.amount)
            .join(PayrollLine, PayrollLine.record_id == PayrollRecord.id)
            .where(PayrollRecord.yyyymm == require_ym(yyyymm), PayrollRecord.confirmed_at.isnot(None),
                   PayrollLine.item_kind == "DEDUCTION",
                   PayrollLine.item_code.in_(list(_ded_key))))).all()
        rec_by: dict[int, dict] = {}
        for emp_id, code, amount in recorded:
            rec_by.setdefault(emp_id, {})[_ded_key[code]] = int(amount)
        worker = {"국민연금": 0, "건강보험": 0, "장기요양": 0, "고용보험": 0, "합계": 0}
        recomputed = 0
        for emp_id, e in per.items():
            if emp_id in rec_by:
                ins = {k: rec_by[emp_id].get(k, 0) for k in ("국민연금", "건강보험", "장기요양", "고용보험")}
                ins["합계"] = sum(ins.values())
            else:
                recomputed += 1
                ins = statutory.insurance(e["salary"], year=year, month=statutory.month_of(yyyymm),
                                          employment_type=e["etype"],
                                          work_days=e["days"] if e["etype"] == "DAILY" else None)
            for k in worker:
                worker[k] += ins[k]
        # 공단납부(추정): 연금·건강·장기요양 노사 50:50(=근로자×2), 고용은 사업주 요율 상이(생략)
        payable = {"국민연금": worker["국민연금"] * 2, "건강보험": worker["건강보험"] * 2,
                   "장기요양": worker["장기요양"] * 2}
        return {"yyyymm": yyyymm, "인원": len(per), "근로자부담": worker,
                "공단납부_추정_노사합산": payable,
                "산출근거": {"공제기록": len(per) - recomputed, "현재요율_재계산": recomputed},
                "주": "고용·산재 사업주 부담분은 사업장 요율 적용 별도. 부가세는 의료용역 면세."}

    async def vat_summary(self, session: AsyncSession, *, start, end) -> dict:
        """부가세 신고 집계 보조 (P2-⑦) — 전자세금계산서 기준 매출·매입 세액 raw.

        의료기관은 의료용역 면세(부가세법 §26①5)라 과세분(미용 등)·매입만 신고 대상.
        공통매입세액 안분·공제율·신고서식은 세무사 확정 — 본 집계는 안분 전 raw(VOID 제외)."""
        from erp.finance.models import TaxInvoice
        rows = (await session.execute(
            select(TaxInvoice.direction, TaxInvoice.invoice_type,
                   func.coalesce(func.sum(TaxInvoice.supply_amount), 0),
                   func.coalesce(func.sum(TaxInvoice.vat_amount), 0),
                   func.count(TaxInvoice.id))
            .where(TaxInvoice.issue_date >= start, TaxInvoice.issue_date <= end,
                   TaxInvoice.status != "VOID")
            .group_by(TaxInvoice.direction, TaxInvoice.invoice_type))).all()
        agg = {(d, t): {"supply": float(s), "vat": float(v), "count": int(c)}
               for d, t, s, v, c in rows}

        def g(d: str, t: str) -> dict:
            return agg.get((d, t), {"supply": 0.0, "vat": 0.0, "count": 0})

        output_vat = g("OUT", "TAX")["vat"]
        input_vat = g("IN", "TAX")["vat"]
        return {
            "start": str(start), "end": str(end),
            "sales": {"taxable": g("OUT", "TAX"), "exempt": g("OUT", "EXEMPT"),
                      "zero": g("OUT", "ZERO")},
            "purchases": {"taxable": g("IN", "TAX"), "exempt": g("IN", "EXEMPT"),
                          "zero": g("IN", "ZERO")},
            "output_vat": output_vat, "input_vat": input_vat,
            "payable_estimate": output_vat - input_vat,
            "주": "의료용역 면세(부가세법 §26①5). 공통매입세액은 과세·면세 안분 후 공제 — "
                  "본 집계는 세금계산서 raw(안분 전). 신고서식·공제율 세무사 확정 필요.",
        }

    async def business_status(self, session: AsyncSession, *, year: int) -> dict:
        """사업장현황신고 집계(면세사업자, 익년 2/10) — 수입·매입·인건비 기초자료.

        수입금액 = 수익계정(REVENUE) 월별 대변−차변(전표 기반 — 수납·보험인식 포함).
        매입 = 전자세금계산서 수취 집계(A-1, 과세/면세). 인건비 = 확정 급여 EARNING 연합계.
        시설현황·면적 등 비계량 항목은 수기 — 본 집계는 수입금액검토표 기초.
        """
        from datetime import date as _date

        from erp.finance.models import Account, Voucher, VoucherLine
        from erp.finance.tax_invoice_service import TaxInvoiceService

        ym = func.substr(cast(Voucher.voucher_on, SAString), 1, 7)
        credit = func.sum(case((VoucherLine.side == "CREDIT", VoucherLine.amount), else_=0))
        debit = func.sum(case((VoucherLine.side == "DEBIT", VoucherLine.amount), else_=0))
        rows = (await session.execute(
            select(ym, Account.code, Account.name, credit - debit)
            .select_from(VoucherLine)
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .join(Account, Account.code == VoucherLine.account_code)
            .where(Account.kind == "REVENUE",
                   Voucher.source_module != "closing",   # 결산분개 제외(수입금액 붕괴 방지)
                   Voucher.voucher_on >= _date(year, 1, 1),
                   Voucher.voucher_on <= _date(year, 12, 31))
            .group_by(ym, Account.code, Account.name)
            .order_by(ym, Account.code))).all()
        revenue = [{"month": m, "account_code": c, "account": n, "amount": float(a or 0)}
                   for m, c, n, a in rows]

        payroll_total = float((await session.execute(
            select(_EARNING).select_from(PayrollLine)
            .join(PayrollRecord, PayrollRecord.id == PayrollLine.record_id)
            .where(PayrollRecord.yyyymm.like(f"{year}-%"),
                   PayrollRecord.confirmed_at.isnot(None)))).scalar_one() or 0)

        purchases = await TaxInvoiceService().summary(session, year=year)
        return {"year": year,
                "revenue": {"items": revenue,
                            "total": sum(r["amount"] for r in revenue)},
                "purchases": purchases,
                "payroll_total": payroll_total,
                "주": "의료용역 면세(부가세법 §26①5) — 과세분(미용 등)은 매출구분 확정 후 분리. "
                      "시설현황 등 비계량 항목은 수기 작성."}

    # medical_expenses(환자별 연간 수납액)는 patient 모듈로 이관(Phase0 결정 2026-07-23) —
    # 환자 chart_no+실명 PHI라 PHI-bound patient 소속. tax는 location-free 유지(patient import 제거).
    # 신규 경로: GET /patients/medical-expenses(.xlsx) · patient.MedicalExpenseReportService.
