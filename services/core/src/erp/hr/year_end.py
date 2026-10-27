"""연말정산 준비 (v1.304.0·2026-10-11) — 귀속 연도 직원별 연간 집계·간이 정산 미리보기·확인할 점.

결정 대기 `year-end-settlement-scope`(외부 / ERP 간이 / ERP 확장)가 1월 전에 정해져야 하는데, 판단 근거(전 직원 연간 총급여·기납부세액·
예상 환급, 근사 산식으로 원천징수된 달, 미확정 급여, 부양가족 미등록)가 화면에 없었다. 어느 방식을 고르든 필요한 자료라 먼저 만든다 —
외부 정산이면 그대로 넘길 자료(엑셀), ERP 정산이면 기준 데이터.

집계 기준은 ESS 원천징수영수증과 같다: 확정 급여 + 성과급 외부 줄(급여 없는 달의 성과급) + 퇴사 정산 연차수당(귀속=퇴사일 연도).
총급여는 비과세(식대 등·월 한도) 제외. 가족 수는 급여 생성과 같은 기준(부양가족 등록 > 보수 프로필).
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_TAX = {"TAX", "INCTAX"}
_LOCAL = {"LOCALTAX", "INCLOCALTAX"}
_PENSION = {"NP", "NP_ADJ"}
_INSURANCE = {"HI", "HI_ADJ", "LTC", "LTC_ADJ", "EI", "EI_ADJ"}
_EXTERNAL = ("INCENTIVE", "INCTAX", "INCLOCALTAX")


def _blank() -> dict:
    return {"gross": 0.0, "nontax": 0.0, "income_tax": 0.0, "local_tax": 0.0, "pension": 0.0, "insurance": 0.0,
            "confirmed_months": set(), "unconfirmed_months": set(), "approx_months": set(), "daily_months": set(),
            "leave_allowance": 0.0}


def _is_daily(r: dict) -> bool:
    """일용(분리과세) 급여 — **기록 자체의 표시**가 먼저(소득세 근거 '일용분리과세'·기본급 근거의 일당), 표시가 없을 때만 지금 고용형태.

    지금 고용형태만 보면 연중 정규→일용으로 바뀐 직원의 정규 달까지 빠진다(검토 LOW). 세액 0이면 소득세 줄이 없어 고용형태를 보조로 쓴다.
    """
    regular_marker = False
    for ln in r["lines"]:
        b = ln.get("calc_basis") or {}
        if ln["item_code"] == "TAX":
            if b.get("method") == "일용분리과세":
                return True
            regular_marker = True
        if ln["item_code"] == "BASE":
            if b.get("daily"):
                return True
            if b:
                regular_marker = True
    return not regular_marker and r.get("etype") == "DAILY"


async def annual_totals(session: AsyncSession, *, year: int, employee_ids: list[int] | None = None) -> dict[int, dict]:
    """직원별 연간 집계 — {employee_id: {gross, nontax, taxable, income_tax, local_tax, pension, insurance, months, ...}}."""
    from erp.hr.models import OffboardingSettlement, PayrollLine, PayrollRecord
    from erp.hr.payroll_rules import load_nontax, nontax_exempt
    nontax = await load_nontax(session)
    conds = [PayrollRecord.yyyymm.like(f"{year}-%")]
    if employee_ids is not None:
        conds.append(PayrollRecord.employee_id.in_(employee_ids))
    from erp.hr.models import Employee
    rows = (await session.execute(
        select(PayrollRecord.id, PayrollRecord.employee_id, PayrollRecord.yyyymm, PayrollRecord.confirmed_at,
               PayrollLine.item_code, PayrollLine.item_kind, PayrollLine.amount, PayrollLine.calc_basis,
               Employee.employment_type)
        .join(PayrollLine, PayrollLine.record_id == PayrollRecord.id)
        .join(Employee, Employee.id == PayrollRecord.employee_id).where(*conds))).all()
    recs: dict[int, dict] = {}
    for rid, emp, ym, conf, code, kind, amt, basis, etype in rows:
        r = recs.setdefault(rid, {"emp": emp, "ym": ym, "confirmed": conf is not None, "lines": [], "etype": etype})
        r["lines"].append({"item_code": code, "item_kind": kind, "amount": float(amt), "calc_basis": basis})
    out: dict[int, dict] = {}
    for r in recs.values():
        t = out.setdefault(r["emp"], _blank())
        if _is_daily(r):
            # 일용근로소득은 분리과세 — 원천징수로 납세가 끝나 연말정산 대상이 아니다(소득세법 §14③2·§73①4). 달만 센다.
            if r["confirmed"]:
                t["daily_months"].add(r["ym"])
            continue
        counted = r["lines"] if r["confirmed"] else [ln for ln in r["lines"] if ln["item_code"] in _EXTERNAL]
        regular = [ln for ln in r["lines"] if ln["item_kind"] == "EARNING" and ln["item_code"] != "INCENTIVE"]
        if not r["confirmed"] and regular:
            t["unconfirmed_months"].add(r["ym"])
        if r["confirmed"] and regular:
            t["confirmed_months"].add(r["ym"])
            tax = next((ln for ln in r["lines"] if ln["item_code"] == "TAX"), None)
            b = (tax or {}).get("calc_basis") or {}
            if tax is not None and not b.get("table_id") and b.get("method") != "일용분리과세":
                t["approx_months"].add(r["ym"])          # 간이세액표가 아닌 근사 산식(또는 수기)으로 원천징수된 달
        for ln in counted:
            c, a = ln["item_code"], ln["amount"]
            if ln["item_kind"] == "EARNING":
                t["gross"] += a
            elif c in _TAX:
                t["income_tax"] += a
            elif c in _LOCAL:
                t["local_tax"] += a
            elif c in _PENSION:
                t["pension"] += a
            elif c in _INSURANCE:
                t["insurance"] += a
        t["nontax"] += nontax_exempt(counted, nontax)
    oq = select(OffboardingSettlement).where(OffboardingSettlement.resigned_on >= date(year, 1, 1),
                                             OffboardingSettlement.resigned_on <= date(year, 12, 31))
    if employee_ids is not None:
        oq = oq.where(OffboardingSettlement.employee_id.in_(employee_ids))
    offs = (await session.scalars(oq)).all()
    for off in offs:
        la = float(off.leave_allowance or 0)
        if la > 0:
            t = out.setdefault(off.employee_id, _blank())
            t["gross"] += la
            t["leave_allowance"] += la
            t["income_tax"] += float(off.leave_allowance_tax or 0)
            t["local_tax"] += float(off.leave_allowance_local_tax or 0)
    for t in out.values():
        t["taxable"] = max(t["gross"] - t["nontax"], 0.0)
        for k in ("confirmed_months", "unconfirmed_months", "approx_months", "daily_months"):
            t[k] = sorted(t[k])
    return out


async def readiness(session: AsyncSession, *, year: int) -> dict:
    """귀속 연도 연말정산 준비 — 직원별 집계·간이 정산 미리보기·확인할 점 + 합계. 읽기 전용."""
    from erp.hr import statutory
    from erp.hr.models import CompensationProfile, Dependent, Employee
    from erp.hr.withholding_table import children_8_20, family_from
    await statutory.refresh_db_params(session)
    totals = await annual_totals(session, year=year)
    ids = list(totals)
    emps = {e.id: e for e in (await session.scalars(select(Employee).where(Employee.id.in_(ids)))).all()} if ids else {}
    deps: dict[int, list] = {}
    for d in (await session.scalars(select(Dependent).where(Dependent.employee_id.in_(ids)))).all() if ids else []:
        deps.setdefault(d.employee_id, []).append(d)
    prof = dict((await session.execute(select(CompensationProfile.employee_id, CompensationProfile.dependents)
                                       .where(CompensationProfile.employee_id.in_(ids)))).all()) if ids else {}
    tax_ok = True
    try:
        statutory._tax_params(year)
    except Exception:  # noqa: BLE001 — 그 해 소득세 파라미터 미등록이면 정산 미리보기만 생략(집계는 보인다)
        tax_ok = False
    rows, sums = [], {"people": 0, "gross": 0.0, "taxable": 0.0, "income_tax": 0.0, "determined": 0.0, "refund": 0.0,
                      "additional": 0.0, "approx_people": 0, "unconfirmed_people": 0, "no_dependents_people": 0}
    for emp_id, t in sorted(totals.items(), key=lambda kv: (emps.get(kv[0]).employee_no if emps.get(kv[0]) else "")):
        e = emps.get(emp_id)
        dl = deps.get(emp_id, [])
        fam = family_from(dl, prof.get(emp_id))
        kids = min(children_8_20(dl, year), fam - 1)
        est = (statutory.year_end_settlement(t["taxable"], withheld_tax=t["income_tax"], year=year, dependents=fam,
                                             children=kids, pension_paid=t["pension"], insurance_paid=t["insurance"])
               if tax_ok and t["taxable"] > 0 else None)      # 일용만 있는 직원은 taxable 0 → 정산 없음
        flags = []
        if t["unconfirmed_months"]:
            flags.append(f"미확정 {len(t['unconfirmed_months'])}개월")
        if t["approx_months"]:
            flags.append(f"근사 산식 원천징수 {len(t['approx_months'])}개월")
        if not dl:
            flags.append("부양가족 미등록(프로필 가족 수)")
        if e is not None and e.resigned_on and e.resigned_on.year == year:
            flags.append(f"중도 퇴사 {e.resigned_on}")
        if e is not None and e.hired_on and e.hired_on.year == year:
            flags.append(f"중도 입사 {e.hired_on}(종전 근무지 합산 필요)")
        if t["daily_months"]:
            flags.append(f"일용(분리과세) {len(t['daily_months'])}개월 제외")
        sums["people"] += 1
        sums["gross"] += t["gross"]
        sums["taxable"] += t["taxable"]
        sums["income_tax"] += t["income_tax"]
        sums["approx_people"] += bool(t["approx_months"])
        sums["unconfirmed_people"] += bool(t["unconfirmed_months"])
        sums["no_dependents_people"] += not dl
        if est:
            sums["determined"] += est["결정세액"]
            if est["정산액"] >= 0:
                sums["refund"] += est["정산액"]
            else:
                sums["additional"] += -est["정산액"]
        rows.append({"employee_id": emp_id, "employee_no": e.employee_no if e else None, "name": e.name if e else None,
                     "gross": round(t["gross"]), "nontax": round(t["nontax"]), "taxable": round(t["taxable"]),
                     "income_tax": round(t["income_tax"]), "local_tax": round(t["local_tax"]),
                     "pension": round(t["pension"]), "insurance": round(t["insurance"]),
                     "months": len(t["confirmed_months"]), "family": fam, "children": kids,
                     "determined": est["결정세액"] if est else None, "settlement": est["정산액"] if est else None,
                     "credit_basis": est.get("공제 방식") if est else None,
                     "flags": flags})
    return {"year": year, "tax_params_registered": tax_ok, "summary": {k: round(v) for k, v in sums.items()}, "rows": rows,
            "note": ("확정 급여(+성과급·퇴사 연차수당) 기준 — ESS 원천징수영수증과 같음. 정산 미리보기는 근로소득공제·인적공제·연금보험료·"
                     "건강·장기요양·고용보험료·근로소득세액공제·자녀세액공제까지(의료비·교육비·기부금·카드·주택자금 등 증빙 공제 미반영 — "
                     "실제 결정세액은 더 작고 환급은 더 클 수 있습니다). +는 환급, −는 추가 납부.")}
