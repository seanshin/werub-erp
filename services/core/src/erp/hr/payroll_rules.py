"""직종 × 고용형태 급여 규칙엔진 (P3) — 명세 라인 자동생성.

직종(의사·간호사·의료기사 등)·고용형태(정규/계약/일용/감시단속)에 따라 기본급 + 직종 수당 +
4대보험·소득세 공제 라인을 deterministic 생성(PreReviewEngine·AllocationEngine 동일 패턴).
수당 단가는 운영 시 단협·취업규칙으로 조정(statutory.RATES와 동일 — 코드 상수).
순수 함수 → PayrollService가 PayrollLine으로 영속.
"""
from erp.hr import statutory

# 직종별 수당 기본값 (운영 조정 대상)
_DUTY_PAY = 100_000              # 당직 1회 (의사·전공의)
_NIGHT_HOURLY = 12_000           # 야간·교대 시간당 (간호사)
_QUALIFICATION_PAY = {           # 자격수당 정액(월) — 의료기사·약사
    "PHARMACIST": 200_000, "RADIOTECH": 150_000, "LABTECH": 120_000,
    "PHYSIOTHERAPIST": 120_000, "TECH": 120_000,
}
_WATCH_ALLOWANCE = 100_000       # 감시단속(경비) 수당
STANDARD_MONTHLY_HOURS = 209     # 월 소정근로시간 (주40h 환산) — 통상시급 분모
_OT_MULTIPLIER = 1.5             # 연장근로 가산 50% (근로기준법 §56)
_HOLIDAY_MULTIPLIER = 1.5        # 휴일근로 8h 이내 가산 50% (근기법 §56② — 계산검토 H-2)
_HOLIDAY_OT_MULTIPLIER = 2.0     # 휴일근로 8h 초과 가산 100% (근기법 §56③)


def _won(x: float) -> str:
    return f"{int(round(x)):,}"


def _earn(code: str, amount: float, basis: dict | None = None) -> dict:
    return {"item_code": code, "item_kind": "EARNING", "amount": int(round(amount)),
            "calc_basis": basis}


def _ded(code: str, amount: float, basis: dict | None = None) -> dict:
    return {"item_code": code, "item_kind": "DEDUCTION", "amount": int(round(amount)),
            "calc_basis": basis}


DEFAULT_RATES = {                # 코드 기본값 — hr.pay_rate_settings(DB)가 우선 (P2-②)
    "DUTY_PAY": _DUTY_PAY, "NIGHT_HOURLY": _NIGHT_HOURLY,
    "WATCH_ALLOWANCE": _WATCH_ALLOWANCE,
    **{f"QUAL_{k}": v for k, v in _QUALIFICATION_PAY.items()},
}


def nontax_exempt(lines, nontax: dict | None) -> float:
    """비과세 공제액 — 지급 줄 중 비과세 항목을 월 한도까지(0/None=전액). 보수월액(4대보험)·월급여액(간이세액표)의 단일 식.

    lines: item_code·amount(·item_kind)를 가진 dict 또는 ORM 줄. nontax={코드: 월 한도}.
    """
    if not nontax:
        return 0.0
    per: dict[str, float] = {}               # 같은 항목이 여러 줄(수기 입력)이어도 월 한도는 항목 합계에 한 번
    for ln in lines:
        code = ln["item_code"] if isinstance(ln, dict) else ln.item_code
        kind = (ln.get("item_kind", "EARNING") if isinstance(ln, dict) else ln.item_kind)
        if kind != "EARNING" or code not in nontax:
            continue
        per[code] = per.get(code, 0.0) + float(ln["amount"] if isinstance(ln, dict) else ln.amount)
    return sum(min(amt, float(nontax[c])) if nontax[c] else amt for c, amt in per.items())


async def load_nontax(session) -> dict:
    """급여 항목 카탈로그의 비과세 지급 항목 → {코드: 월 한도}."""
    from sqlalchemy import select

    from erp.hr.models import PayItemCatalog
    return {c: float(lim or 0) for c, lim in (await session.execute(select(
        PayItemCatalog.code, PayItemCatalog.nontax_limit).where(
        PayItemCatalog.kind == "EARNING", PayItemCatalog.taxable.is_(False)))).all()}


def table_tax_basis(table: dict, taxable: float, dependents: int, children: int) -> dict:
    """간이세액표 소득세 줄 계산근거 — 급여 생성과 세액 재계산(withholding_table.retax)의 단일 소스."""
    eff = table["effective_from"]
    return {"formula": f"간이세액표 {eff} 시행(월급여 {_won(taxable)}·가족 {dependents}인"
                       + (f"·자녀 {children}명" if children else "") + ")",
            "method": "간이세액표", "dependents": dependents, "children": children,
            "taxable": int(taxable), "table_id": table["id"]}


def build_payroll(*, staff_class: str | None = None, job_category: str | None = None,
                  employment_type: str = "REGULAR", base_pay: float = 0.0,
                  daily_wage: float = 0.0, work_days: int = 0, work_hours: float = 0.0,
                  night_hours: float = 0.0, duty_count: int = 0, dependents: int = 1,
                  ot_hours: float = 0.0, holiday_hours: float = 0.0,
                  holiday_ot_hours: float = 0.0,
                  unpaid_days: float = 0.0, year: int = 2026, month: int | None = None,
                  rates: dict | None = None, night_pay_mode: str = "FLAT",
                  employed_days: int | None = None, month_days: int | None = None,
                  withholding_table: dict | None = None, children: int = 0,
                  nontax: dict | None = None) -> dict:
    """명세 생성 — earnings/deductions 라인 + gross/공제계/net.

    rates: DB 수당 단가(미지정 키는 코드 기본값) — 순수성 유지(호출자가 로드해 주입).
    employed_days/month_days: 입사·퇴사월 기본급 일할(2026-10-09 전체 흐름 검토 C).
      **역일(달력일) 기준** — 기본급 × 재직 역일수 ÷ 해당월 역일수. 재직 역일 = max(입사일, 월초)
      ~ min(퇴사일, 월말) 양끝 포함(퇴사일=최종 근무일로 해석). 둘 다 주어지고 재직일<월일수일 때만
      적용. 통상시급(연장·휴일·야간·무급공제 단가)은 월 전액 기본급 기준 유지 — 시급은 월급÷209h로
      정의되므로 일할된 금액으로 나누면 단가가 깎인다. 정액 수당(자격·감시단속)은 일할 대상 아님(현행).
    """
    r = {**DEFAULT_RATES, **(rates or {})}
    earnings: list[dict] = []
    # 기본급 — 일용직은 일당×일수, 그 외 월 기본급 (calc_basis: 계산방법 표기·§27의2 5호)
    if employment_type == "DAILY":
        earnings.append(_earn("BASE", daily_wage * work_days,
                              {"formula": f"{_won(daily_wage)} × {work_days}일",
                               "daily": int(round(daily_wage)), "days": work_days}))
    elif employed_days is not None and month_days and 0 <= employed_days < month_days:
        earnings.append(_earn("BASE", base_pay * employed_days / month_days,   # 입사·퇴사월 일할
                              {"formula": f"{_won(base_pay)} × {employed_days}/{month_days}일(역일 일할)",
                               "monthly": int(round(base_pay)), "days": employed_days,
                               "month_days": month_days, "method": "CALENDAR_DAYS"}))
    else:
        earnings.append(_earn("BASE", base_pay))                       # 월정액(계산식 없음)
    # 직종 수당
    if job_category in ("DOCTOR", "RESIDENT") and duty_count:
        earnings.append(_earn("DUTY", duty_count * r["DUTY_PAY"],      # 당직비
                              {"formula": f"{duty_count}회 × {_won(r['DUTY_PAY'])}",
                               "count": duty_count, "unit": int(r["DUTY_PAY"])}))
    if job_category == "NURSE" and night_hours:
        # 야간수당 산정 방식(M-11·PayrollSettings.night_pay_mode): FLAT=정액단가(현행) |
        # STATUTORY=통상시급×0.5 가산(근기법 §56③ — 야간 기본임금은 월급에 포함, 가산분만) |
        # MAX=둘 중 큰 값(정액이 법정 가산에 미달하는 고임금 간호사 보호).
        flat_amt = night_hours * r["NIGHT_HOURLY"]
        stat_hourly = (base_pay / STANDARD_MONTHLY_HOURS * 0.5) if base_pay else 0.0
        stat_amt = night_hours * stat_hourly
        mode = (night_pay_mode or "FLAT").upper()
        if mode == "STATUTORY":
            amt, formula = stat_amt, f"{night_hours}h × {_won(stat_hourly)}(통상시급×0.5)"
        elif mode == "MAX":
            amt = max(flat_amt, stat_amt)
            formula = (f"max(정액 {_won(flat_amt)}, 법정 {_won(stat_amt)})")
        else:
            amt, formula = flat_amt, f"{night_hours}h × {_won(r['NIGHT_HOURLY'])}"
        earnings.append(_earn("NIGHT", amt,
                              {"formula": formula, "hours": night_hours, "mode": mode,
                               "unit": int(r["NIGHT_HOURLY"])}))
    qual = r.get(f"QUAL_{job_category}", 0)
    if qual:
        earnings.append(_earn("QUAL", qual))                           # 자격수당(정액)
    if employment_type == "WATCH" or job_category == "SECURITY":
        earnings.append(_earn("WATCH", r["WATCH_ALLOWANCE"]))          # 감시단속수당(정액)
    # 연장근로수당 — 통상시급(기본급/209h) × 1.5 (일용·감시단속 제외: 일당·포괄 정산)
    if ot_hours and base_pay and employment_type not in ("DAILY", "WATCH"):
        hourly = base_pay / STANDARD_MONTHLY_HOURS
        earnings.append(_earn("OT", ot_hours * hourly * _OT_MULTIPLIER,
                              {"formula": f"{ot_hours}h × {_won(hourly)} × {_OT_MULTIPLIER}",
                               "hours": ot_hours, "hourly": int(round(hourly)),
                               "rate": _OT_MULTIPLIER}))

    # 휴일근로수당(H-2) — 근기법 §56②③: 8h 이내 ×1.5, 초과 ×2.0 (OT 라인과 동형 전액 방식·
    # 일용·감시단속 제외). 시간 산정은 pay_inputs가 일별 스테이징에서 휴일(일요일+법정공휴일)만 분리.
    if (holiday_hours or holiday_ot_hours) and base_pay \
            and employment_type not in ("DAILY", "WATCH"):
        hourly = base_pay / STANDARD_MONTHLY_HOURS
        amount = (holiday_hours * hourly * _HOLIDAY_MULTIPLIER
                  + holiday_ot_hours * hourly * _HOLIDAY_OT_MULTIPLIER)
        formula = f"{holiday_hours}h × {_won(hourly)} × {_HOLIDAY_MULTIPLIER}"
        if holiday_ot_hours:
            formula += f" + {holiday_ot_hours}h × {_won(hourly)} × {_HOLIDAY_OT_MULTIPLIER}"
        earnings.append(_earn("HOLIDAY", amount,
                              {"formula": formula, "hours": holiday_hours,
                               "ot_hours": holiday_ot_hours, "hourly": int(round(hourly)),
                               "rate": _HOLIDAY_MULTIPLIER, "ot_rate": _HOLIDAY_OT_MULTIPLIER}))

    # 무급휴가 공제 — 일할(통상일급=기본급/209h×8h) (HIS 합의 2026-06-11, 일 단위)
    # 2026-10-09 전체 흐름 검토 C — 상한 = 당월 지급 기본급(BASE 라인). 통상일급은 소정근로일
    # 기준(209h÷8h ≈ 26.1일)이라 역일 기준 무급일수(최대 31일)를 곱하면 기본급을 넘어 gross가 음수가
    # 될 수 있었다(한 달 전부 무급휴직 → 음수 급여·음수 보험료 기준). 기본급까지만 차감하고 수당은 유지.
    if unpaid_days and base_pay and employment_type not in ("DAILY", "WATCH"):
        daily = base_pay / STANDARD_MONTHLY_HOURS * 8
        raw = daily * unpaid_days
        cap = float(earnings[0]["amount"])                             # 당월 BASE(일할 반영)
        amt = min(raw, max(cap, 0.0))
        basis = {"formula": f"−{unpaid_days}일 × {_won(daily)}",
                 "days": unpaid_days, "daily": int(round(daily))}
        if amt < raw:
            basis["formula"] += f" (기본급 {_won(cap)} 한도)"
            basis["capped"] = True
        earnings.append(_earn("UNPAID_LEAVE", -amt, basis))           # 지급액 차감(소득 감액)

    gross = sum(e["amount"] for e in earnings)
    # 비과세(식대 등·월 한도) — 보수월액(4대보험)·월급여액(간이세액표) 모두 비과세 제외(v1.303: 종전 4대보험은 총액 기준)
    insurable = max(gross - nontax_exempt(earnings, nontax), 0)

    # 공제 — 4대보험(고용형태 특례 P2) + 소득세 (calc_basis: 기준액×요율 표기)
    deductions: list[dict] = []
    ins = statutory.insurance(insurable, year=year, employment_type=employment_type,
                              work_days=work_days or None, work_hours=work_hours or None,
                              with_basis=True, month=month)
    _basis = ins.get("_basis", {})
    for code, key in (("NP", "국민연금"), ("HI", "건강보험"),
                      ("LTC", "장기요양"), ("EI", "고용보험")):
        if ins[key]:
            b = _basis.get(code, {})
            deductions.append(_ded(code, ins[key],
                                   {"formula": f"{_won(b.get('base', 0))} × {b.get('rate', 0) * 100:g}%",
                                    "base": int(round(b.get("base", 0))), "rate": b.get("rate", 0)}))
    if employment_type == "DAILY":
        tax = statutory.daily_withholding(daily_wage, days=work_days, year=year)
        income_tax, local_tax = tax["소득세"], tax["지방소득세"]
        tax_basis = {"formula": f"일용 {work_days}일 분리과세", "method": "일용분리과세"}
    else:
        # 월급여액 = 비과세 제외(별표 2 '비과세 및 학자금 제외') — 위 exempt(보수월액과 같은 기준)
        taxable = insurable
        if withholding_table is not None:   # v1.297.0 — 활성 간이세액표(사람이 원표 대조·활성화한 것)
            from erp.hr import withholding_table as wht
            tax = wht.compute(withholding_table, taxable, family=dependents, children=children)
            tax_basis = table_tax_basis(withholding_table, taxable, dependents, children)
        else:                                # 활성 표 없는 기간 — 종전 근사 산식(표기를 사실대로)
            tax = statutory.estimate_monthly_income_tax(taxable, dependents=dependents, year=year)
            tax_basis = {"formula": f"근사 산식(간이세액표 미등록·부양 {dependents}인)", "method": "근사산식",
                         "dependents": dependents, "taxable": int(taxable)}
        income_tax, local_tax = tax["소득세"], tax["지방소득세"]
    if income_tax:
        deductions.append(_ded("TAX", income_tax, tax_basis))
    if local_tax:
        deductions.append(_ded("LOCALTAX", local_tax,
                               {"formula": "소득세 × 10%", "rate": 0.1}))

    ded_total = sum(d["amount"] for d in deductions)
    return {"lines": earnings + deductions, "gross": gross,
            "deduction_total": ded_total, "net": gross - ded_total}
