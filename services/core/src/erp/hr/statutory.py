"""급여 법정처리 (FR-07 확장) — 4대보험·퇴직금·연말정산 계산.

요율·구간은 매년 고시(국세청·공단)로 갱신 — RATES/공제표는 연도별 상수(운영 시 갱신).
4대보험 근로자 부담분만(산재는 사업주 전액). 원단위 절사(10원 미만 버림).
"""
from datetime import date

# 연도별 4대보험 요율 (근로자 부담) — 고시값(운영 시 매년 갱신·관리화면 등록이 우선)
# 국민연금 기준소득월액 상·하한은 **7월분부터 1년** 적용이라 한 해에 두 구간이 있다 — `*_h2`=7~12월분.
# 2026-10-10: 2026 값이 2025 복사본(4.5%·3.545%·12.95%)이었다 — 그 사이 확정한 급여가 그 요율로 지급됨
# (결정 대기 statutory-2026-rates-check). 출처: 복지부 보도자료 "2026년 건강보험료율 7.19%로 결정"·
# "2026년도 장기요양보험료율 0.9448%"(=건보료×0.9448/7.19=13.14%)·연금개혁(2026 9.5%)·
# 공단 기준소득월액 상·하한(2025.7~2026.6 40만/637만 · 2026.7~2027.6 41만/659만).
RATES = {
    2025: {"pension": 0.045, "pension_min": 390000, "pension_max": 6170000,
           "pension_min_h2": 400000, "pension_max_h2": 6370000,
           "health": 0.03545, "longterm_of_health": 0.1295, "employment": 0.009},
    2026: {"pension": 0.0475, "pension_min": 400000, "pension_max": 6370000,
           "pension_min_h2": 410000, "pension_max_h2": 6590000,
           "health": 0.03595, "longterm_of_health": 0.1314, "employment": 0.009},
}

# 연도별 소득세 파라미터(국세청 고시 — 매년 갱신·계산검토 H-1: 4대보험 RATES와 대칭 fail-safe).
# 미등록 연도는 ValidationError로 갱신을 강제해 세법 개정 시 무음 구식 세액 산출을 차단.
_TAX_2026 = {
    # 근로소득공제 구간: (상한, 누적공제, 한계율)
    "earned_deduction": [
        (5_000_000, 0, 0.70),
        (15_000_000, 3_500_000, 0.40),
        (45_000_000, 7_500_000, 0.15),
        (100_000_000, 12_000_000, 0.05),
        (float("inf"), 14_750_000, 0.02),
    ],
    "earned_deduction_cap": 20_000_000,   # 근로소득공제 한도(소득세법 §47① 단서·2020 귀속~)
    # 종합소득세 과세표준 구간: (상한, 세율, 누진공제)
    "brackets": [
        (14_000_000, 0.06, 0),
        (50_000_000, 0.15, 1_260_000),
        (88_000_000, 0.24, 5_760_000),
        (150_000_000, 0.35, 15_440_000),
        (300_000_000, 0.38, 19_940_000),
        (500_000_000, 0.40, 25_940_000),
        (1_000_000_000, 0.42, 35_940_000),
        (float("inf"), 0.45, 65_940_000),
    ],
    # 근로소득세액공제(§59): 55%/30% 임계·기본액 + 총급여별 한도 (경계, 기준액, 감액률, 하한)
    "eitc_threshold": 1_300_000, "eitc_base": 715_000,
    "eitc_rate_low": 0.55, "eitc_rate_high": 0.30,
    "eitc_limits": [
        (33_000_000, 740_000, 0.0, 740_000),
        (70_000_000, 740_000, 0.008, 660_000),
        (120_000_000, 660_000, 0.5, 500_000),
        (float("inf"), 500_000, 0.5, 200_000),
    ],
    "personal_deduction": 1_500_000,   # 기본 인적공제(1인)
    # 자녀세액공제(8~20세, 소득세법 §59의2): 1명·2명(누적)·3명째부터 인당 — 2024.12 개정으로 2025 귀속분부터
    # 첫째 25만·둘째 30만·셋째 이후 40만(누적 25만/55만/+40만). 종전 15만/35만/+30만은 2024 귀속까지 값이었다(2026-10-10).
    "child_first": 250_000, "child_second": 550_000, "child_extra": 400_000,
    # 일용근로 분리과세: 일 공제·세율·세액공제율·소액부징수 기준
    "daily_deduction": 150_000, "daily_rate": 0.06,
    "daily_credit": 0.55, "daily_min_tax": 1_000,
    # 퇴직소득세(소득세법 §48·§55②) — 2026-10-09 전체 흐름 검토 C에서 신설. 근거: 2022.12.31 개정
    # 소득세법(2023.1.1 이후 퇴직분 적용) 근속연수공제표(§48①2호) + 2020.1.1 이후 환산급여공제표
    # (§48①1호). 2026년까지 개정 없음 전제 — 세법 개정 시 이 표를 갱신(⚠세무 검토 대기 항목).
    # 근속연수공제: (근속연수 상한, 누적공제, 초과 1년당 공제)
    "retire_service_deduction": [
        (5, 0, 1_000_000),
        (10, 5_000_000, 2_000_000),
        (20, 15_000_000, 2_500_000),
        (float("inf"), 40_000_000, 3_000_000),
    ],
    # 환산급여공제: (환산급여 상한, 누적공제, 초과분 공제율)
    "retire_conv_deduction": [
        (8_000_000, 0, 1.00),
        (70_000_000, 8_000_000, 0.60),
        (100_000_000, 45_200_000, 0.55),
        (300_000_000, 61_700_000, 0.45),
        (float("inf"), 151_700_000, 0.35),
    ],
}
TAX_PARAMS: dict[int, dict] = {2025: _TAX_2026, 2026: _TAX_2026}   # 2025·2026 동일(개정 없음)


# ── 관리화면 등록분(DB·hr.statutory_params·마이그 0250) — 코드 상수보다 우선 ─────────────
# 매년 고시 요율을 **코드 배포 없이** 관리자가 근거와 함께 등록한다(2027-01-01 원천징수 정지 예방).
# 계산 함수는 동기라 프로세스 캐시를 쓴다 — core 기동 시·관리자 저장 시·급여/세액 계산 진입점에서
# refresh_db_params()로 갱신(core는 단일 프로세스·워커는 법정 계산을 하지 않는다).
_DB_INSURANCE: dict[int, dict] = {}     # year → RATES와 같은 키
_DB_TAX_CARRY: dict[int, int] = {}      # year → 승계 원본 연도(세법 개정 없음 확인)
#: year → {"from_year": N, "overrides": {키: 값}} — 세법 개정분만 원본 연도 위에 덮어쓴다(v1.300·코드 배포 없이 개정 반영)
_DB_TAX_AMEND: dict[int, dict] = {}
_TAX_AMEND_RESOLVED: dict[int, dict] = {}
INSURANCE_KEYS = ("pension", "pension_min", "pension_max", "health", "longterm_of_health",
                  "employment")
#: 선택 — 7월분부터 바뀌는 국민연금 상·하한(없으면 연중 같은 상·하한)
INSURANCE_OPTIONAL_KEYS = ("pension_min_h2", "pension_max_h2")
#: 선택 — 그 해 사업주 고용보험(실업급여+고용안정·직업능력개발)·산재 요율(v1.297). 없으면 급여 설정의 단일 값(연도 무관) —
#: 종전엔 연도 구분이 없어 값을 바꾸면 과거 연도 확정분 재계산까지 바뀌고, 매년 고시 변경을 놓쳐도 경보가 없었다.
EMPLOYER_OPTIONAL_KEYS = ("employer_ei", "employer_wc")
#: 선택 — 그 해 최저임금 시급(원·v1.303). 코드 값보다 우선(고시가 바뀌면 화면 등록).
WAGE_OPTIONAL_KEYS = ("min_wage_hourly",)
#: 최저임금 시급(고용노동부 고시) — 2025 10,030 · 2026 10,320 · 2027 10,700(2026-08-05 고시). 월 환산=×209h.
MIN_WAGE_HOURLY: dict[int, int] = {2025: 10_030, 2026: 10_320, 2027: 10_700}


def min_wage_hourly(year: int) -> tuple[int | None, str]:
    """(그 해 최저임금 시급, 출처) — 관리화면 등록 > 코드 상수 > 없음(None — 검사 생략을 화면에 드러낸다)."""
    db = (_DB_INSURANCE.get(year) or {}).get("min_wage_hourly")
    if db:
        return int(db), "법정 요율 등록"
    if year in MIN_WAGE_HOURLY:
        return MIN_WAGE_HOURLY[year], "코드 고시값"
    return None, "미등록"


def employer_rates(year: int, *, fallback_ei: float, fallback_wc: float) -> tuple[float, float, str]:
    """(고용보험 사업주, 산재, 출처) — 그 해 법정 요율 등록값 > 급여 설정 값."""
    try:
        r = insurance_rates(year)
    except Exception:  # noqa: BLE001 — 미등록 연도는 급여 계산 자체가 막히므로 여기선 폴백만
        r = {}
    ei, wc = r.get("employer_ei"), r.get("employer_wc")
    src = "법정 요율 등록" if ei is not None or wc is not None else "급여 설정(연도 무관)"
    return (float(ei) if ei is not None else float(fallback_ei),
            float(wc) if wc is not None else float(fallback_wc), src)


def month_of(yyyymm: str) -> int:
    """급여월 문자열의 월 — "2026-07"·"202607" 모두(라우터가 둘 다 받는다·슬라이스 [5:7]은 압축형에서 틀린다)."""
    return int(str(yyyymm).replace("-", "")[4:6])


def pension_bounds(r: dict, month: int | None) -> tuple[float, float]:
    """국민연금 기준소득월액 (하한, 상한) — 7월분부터 하반기 구간(등록돼 있을 때)."""
    if month is not None and month >= 7 and r.get("pension_min_h2") and r.get("pension_max_h2"):
        return r["pension_min_h2"], r["pension_max_h2"]
    return r["pension_min"], r["pension_max"]


def insurance_rates(year: int) -> dict:
    """연도 4대보험 요율 — 관리화면 등록 > 코드 상수 > 미등록 예외(무음 폴백 금지)."""
    if year in _DB_INSURANCE:
        return _DB_INSURANCE[year]
    if year in RATES:
        return RATES[year]
    from erp.errors import ValidationError
    raise ValidationError(
        f"{year}년 4대보험 요율 미등록 — 관리자(ADMIN)가 /admin/payroll-settings '법정 파라미터'에서 "
        f"고시 요율을 등록해야 합니다 (현재 등록: {registered_years()['insurance']})")


def registered_years() -> dict:
    return {"insurance": sorted(set(RATES) | set(_DB_INSURANCE)),
            "tax": sorted(set(TAX_PARAMS) | set(_DB_TAX_CARRY) | set(_DB_TAX_AMEND))}


async def refresh_db_params(session) -> None:
    """DB 등록분을 캐시에 반영 — 표가 작아(연 2행) 계산 진입점마다 불러도 싸다."""
    from sqlalchemy import select

    from erp.hr.models import StatutoryParam
    rows = (await session.scalars(select(StatutoryParam))).all()
    ins = {r.year: dict(r.values) for r in rows if r.kind == "INSURANCE"}
    carry = {r.year: int(r.values["from_year"]) for r in rows if r.kind == "TAX_CARRY"}
    amend = {r.year: {"from_year": int(r.values["from_year"]), "overrides": dict(r.values.get("overrides") or {})}
             for r in rows if r.kind == "TAX_AMEND"}
    _DB_INSURANCE.clear()
    _DB_INSURANCE.update(ins)
    _DB_TAX_CARRY.clear()
    _DB_TAX_CARRY.update(carry)
    _DB_TAX_AMEND.clear()
    _DB_TAX_AMEND.update(amend)
    _TAX_AMEND_RESOLVED.clear()
    import time
    _last_refresh[0] = time.monotonic()


_REFRESH_TTL = 60.0
_last_refresh = [float("-inf")]


def _stale() -> bool:
    import time
    return time.monotonic() - _last_refresh[0] >= _REFRESH_TTL


def invalidate_cache() -> None:
    """다음 요청에서 다시 읽게 한다 — 저장 직후(커밋 뒤) 호출."""
    _last_refresh[0] = float("-inf")


async def _hook_run(session) -> None:
    """실패해도 TTL을 소비한다 — 실패가 이어지는 동안 매 요청 SAVEPOINT+실패 쿼리를 반복하지 않게
    (2차 검토). 실패하면 직전 캐시가 유지된다."""
    import time
    _last_refresh[0] = time.monotonic()
    await refresh_db_params(session)


def _register_hook() -> None:
    from erp.db import register_session_hook
    register_session_hook(_stale, _hook_run)


_register_hook()


def _tax_params(year: int | None) -> dict:
    """연도 소득세 파라미터 — 미등록 연도 무음폴백 금지(RATES 대칭 fail-safe·계산검토 H-1).

    코드 TAX_PARAMS > 관리화면 '개정 반영'(원본 연도 + 바뀐 항목) > '전년 승계'(개정 없음 확인 결정) > 미등록 예외.
    """
    y = year if year is not None else date.today().year
    if y in TAX_PARAMS:
        return TAX_PARAMS[y]
    am = _DB_TAX_AMEND.get(y)
    if am is not None and am["from_year"] != y:
        if y not in _TAX_AMEND_RESOLVED:
            base = dict(_tax_params(am["from_year"]))
            base.update({k: tax_value_from_json(k, v, base) for k, v in am["overrides"].items()})
            _TAX_AMEND_RESOLVED[y] = base
        return _TAX_AMEND_RESOLVED[y]
    src = _DB_TAX_CARRY.get(y)
    if src is not None and src != y:
        return _tax_params(src)
    from erp.errors import ValidationError
    raise ValidationError(
        f"{y}년 소득세 파라미터 미등록 — 세법 개정이 없으면 관리화면 /admin/payroll-settings에서 "
        f"'전년 승계'를 확정하고, 개정이 있으면 같은 화면 '개정 반영'으로 바뀐 항목을 등록 "
        f"(현재 등록: {registered_years()['tax']})")


def tax_value_to_json(v):
    """소득세 파라미터 값 → JSON(구간표 튜플은 리스트·상한 무한대는 null)."""
    if isinstance(v, (list, tuple)):
        return [tax_value_to_json(x) for x in v]
    if isinstance(v, float) and v == float("inf"):
        return None
    return v


def tax_value_from_json(key: str, v, base: dict):
    """JSON → 계산용 값 — 원본과 같은 모양(구간표=튜플 행·null=무한대). 검증은 statutory_params.amend_tax."""
    if isinstance(base.get(key), list):
        return [tuple(float("inf") if x is None else x for x in row) for row in v]
    return v


def tax_params_json(year: int) -> dict:
    """화면 프리필용 — 그 해 소득세 파라미터 전체(JSON 모양)."""
    return {k: tax_value_to_json(v) for k, v in _tax_params(year).items()}


def _trunc10(x: float) -> int:
    """원단위 절사 (10원 미만 버림) — float 오차 제거 위해 원단위 반올림 후 절사."""
    return int(round(x) // 10 * 10)


# 일용근로자 국민연금·건강보험 적용기준: 1개월 이상 AND (월 8일↑ OR 60시간↑). 미만 시 적용제외.
_DAILY_NPHEALTH_DAYS = 8
_DAILY_NPHEALTH_HOURS = 60
# 일용근로 분리과세 상수는 TAX_PARAMS(daily_*)로 연도화 — 계산검토 H-1.


def insurance(monthly_salary: float, *, year: int = 2026, employment_type: str = "REGULAR",
              work_days: int | None = None, work_hours: float | None = None,
              with_basis: bool = False, month: int | None = None) -> dict:
    """4대보험 근로자 부담분 — 보수월액 기준. 국민연금 상·하한 적용.

    고용형태 특례(P2): 일용직(DAILY)은 국민연금·건강보험을 월 8일↑ 또는 60h↑ 일 때만 가입
    (미만 시 적용제외, 0). 고용보험은 일용 포함 적용. 산재는 사업주 전액(근로자 0).
    감시단속(WATCH)·계약·임시는 상용근로자와 동일 적용.
    """
    r = insurance_rates(year)   # fail-safe — 미등록 연도 무음폴백(잘못된 요율) 금지
    if monthly_salary <= 0:   # 전월 무급휴가 등 보수월액 0/음수 — 보험료 미부과(음수 보험료·하한 오부과 방지)
        result = {"국민연금": 0, "건강보험": 0, "장기요양": 0, "고용보험": 0, "합계": 0}
        if with_basis:
            result["_basis"] = {k: {"base": 0, "rate": r[v]} for k, v in
                                (("NP", "pension"), ("HI", "health"),
                                 ("LTC", "longterm_of_health"), ("EI", "employment"))}
        return result
    employment = _trunc10(monthly_salary * r["employment"])   # 고용보험 — 일용 포함
    np_health_applies = True
    if employment_type == "DAILY":
        np_health_applies = ((work_days or 0) >= _DAILY_NPHEALTH_DAYS
                             or (work_hours or 0) >= _DAILY_NPHEALTH_HOURS)
    if np_health_applies:
        lo, hi = pension_bounds(r, month)
        np_base = min(max(monthly_salary, lo), hi)
        pension = _trunc10(np_base * r["pension"])
        health = _trunc10(monthly_salary * r["health"])
        longterm = _trunc10(health * r["longterm_of_health"])
    else:
        pension = health = longterm = 0
    total = pension + health + longterm + employment
    result = {"국민연금": pension, "건강보험": health, "장기요양": longterm,
              "고용보험": employment, "합계": total}
    if with_basis:   # 명세서 계산방법 표기용 항목별 기준액·요율(표시 전용·P1)
        lo, hi = pension_bounds(r, month)
        np_base = min(max(monthly_salary, lo), hi) if np_health_applies else 0
        result["_basis"] = {
            "NP": {"base": np_base, "rate": r["pension"]},
            "HI": {"base": (monthly_salary if np_health_applies else 0), "rate": r["health"]},
            "LTC": {"base": (health if np_health_applies else 0), "rate": r["longterm_of_health"]},
            "EI": {"base": monthly_salary, "rate": r["employment"]},
        }
    return result


def employer_burden(monthly_salary: float, *, year: int = 2026, employment_type: str = "REGULAR",
                    work_days: int | None = None, work_hours: float | None = None,
                    ei_rate: float = 0.0115, wc_rate: float = 0.007,
                    month: int | None = None) -> dict:
    """4대보험 사업주 부담분 — 국민연금·건강·장기요양은 근로자와 동률, 고용보험(사업주 요율)·산재(전액).

    ei_rate=고용보험 사업주 요율(실업급여 0.9%+고용안정·직능), wc_rate=산재 요율(업종별) — 설정값.
    """
    ee = insurance(monthly_salary, year=year, employment_type=employment_type,
                   work_days=work_days, work_hours=work_hours, month=month)
    employment = _trunc10(monthly_salary * ei_rate)   # 고용보험 사업주
    accident = _trunc10(monthly_salary * wc_rate)      # 산재(사업주 전액)
    pension, health, longterm = ee["국민연금"], ee["건강보험"], ee["장기요양"]  # 사업주 동률
    total = pension + health + longterm + employment + accident
    return {"국민연금": pension, "건강보험": health, "장기요양": longterm,
            "고용보험": employment, "산재": accident, "합계": total}


def bonus_income_tax(monthly_regular: float, bonus: float, *, months: int = 1,
                     dependents: int = 1, prepaid_monthly_tax: float | None = None,
                     year: int | None = None, table: dict | None = None, children: int = 0) -> dict:
    """상여 세액(소득세법 시행령 §194 방식) — (A × B) − C.

    A = (지급대상기간 급여+상여)÷월수 = 월급+상여/월수 에 대한 간이세액, B = 월수,
    C = 지급대상기간 기납부세액. prepaid_monthly_tax(당월 실제 원천징수 소득세)를 주면 그것으로 C를
    산정(정식), 없으면 월급 재계산 세액으로 근사. 간이세액표는 estimate_monthly_income_tax로 근사
    (완전 정밀은 국세청 간이세액표 적재 필요). 지방소득세 = 소득세 10%.
    """
    months = max(int(months), 1)
    if table is not None:     # v1.297.0 — 활성 간이세액표가 있으면 A·C 모두 표로(월 급여 TAX와 같은 기준이어야 A−C가 맞다)
        from erp.hr import withholding_table as _wht

        def _m(x: float) -> float:
            return _wht.compute(table, x, family=dependents, children=children)["소득세"]
    else:
        def _m(x: float) -> float:
            return estimate_monthly_income_tax(x, dependents=dependents, year=year)["소득세"]
    a = _m(monthly_regular + bonus / months)
    c = prepaid_monthly_tax if prepaid_monthly_tax is not None else _m(monthly_regular)
    income_tax = max(_trunc10((a - c) * months), 0)
    return {"소득세": income_tax, "지방소득세": _trunc10(income_tax * 0.1)}


def daily_withholding(daily_wage: float, *, days: int = 1, year: int | None = None) -> dict:
    """일용근로 소득세 원천징수(분리과세) — 일별 (일급−일공제)×세율×(1−세액공제), 소액부징수.

    지방소득세 = 소득세 10%. 일급이 일공제(15만) 이하면 소득세 0. 상수는 TAX_PARAMS 연도값(H-1).
    """
    p = _tax_params(year)
    per_day_base = max(daily_wage - p["daily_deduction"], 0)
    per_day_raw = per_day_base * p["daily_rate"] * (1 - p["daily_credit"])
    # 소액부징수(소득세법 §86)는 '지급 시 세액 합계' 기준 — 며칠분 일괄지급 시 합계로 판단(국세청
    # 유권해석). 일 단위로 1,000원 미만 판정하면 일세액 27~999원 구간이 며칠을 일괄지급해도 0으로
    # 전액 과소징수(적대검토 M-A). 절사도 합계에 적용.
    total_raw = per_day_raw * days
    income_tax = 0 if total_raw < p["daily_min_tax"] else _trunc10(total_raw)
    local_tax = _trunc10(income_tax * 0.1)
    per_day_tax = _trunc10(per_day_raw)   # 표시용(참고) — 실제 징수는 합계 기준
    return {"일급": daily_wage, "일수": days, "일소득세": per_day_tax,
            "소득세": income_tax, "지방소득세": local_tax, "합계": income_tax + local_tax}


def severance(avg_monthly_wage: float, *, hire_date: date, as_of: date) -> dict:
    """퇴직금 추정 — 1년 미만 미지급. ≈ 1개월 평균임금 × 근속연수(일할).

    as_of는 마지막 재직일(양끝 포함 — `service_years`·급여 일할·평균임금 기간과 같은 규약).
    2026-10-09 시나리오 3회차: 종전 `(as_of - hire_date).days`는 퇴사일을 빼 퇴직금이 하루분 적었고,
    입사 2025-10-01·퇴사 2026-09-30(정확히 1년)이 364일로 지급대상에서 빠졌다.
    """
    days = (as_of - hire_date).days + 1
    if days < 365:
        return {"근속일수": days, "지급대상": False, "퇴직금": 0}
    amount = round(avg_monthly_wage * (days / 365))
    return {"근속일수": days, "지급대상": True, "퇴직금": amount}


def service_years(hire_date: date, retire_date: date) -> int:
    """퇴직소득 근속연수 — 1년 미만 끝수는 1년으로(소득세법 시행령 §105①). 최소 1년.

    retire_date는 최종 근무일(양끝 포함) — 2016-01-01 입사·2025-12-31 퇴사 = 정확히 10년."""
    from datetime import timedelta
    end = retire_date + timedelta(days=1)                 # 근속 종료(배타)
    years = end.year - hire_date.year
    if (end.month, end.day) < (hire_date.month, hire_date.day):
        years -= 1
    exact = (end.month, end.day) == (hire_date.month, hire_date.day)
    return max(years + (0 if exact else 1), 1)


def retirement_income_tax(retirement_pay: float, *, years: int, year: int | None = None) -> dict:
    """퇴직소득세 원천징수(소득세법 §48·§55②·2026-10-09 전체 흐름 검토 C).

    ① 근속연수공제 → ② 환산급여 = (퇴직소득금액 − 근속연수공제) × 12 ÷ 근속연수 → ③ 환산급여공제
    → ④ 과세표준 = 환산급여 − 환산급여공제 → ⑤ 환산산출세액 = 기본세율(종합소득세율표) 적용
    → ⑥ 산출세액 = 환산산출세액 × 근속연수 ÷ 12. 지방소득세 = 산출세액 × 10%. 10원 미만 절사.
    비과세 퇴직소득·세액공제·이연퇴직소득(IRP 이체 과세이연)은 미반영 — 해당 시 정산 화면 밖 수기 처리.
    """
    p = _tax_params(year)
    n = max(int(years), 1)
    income = max(float(retirement_pay), 0.0)
    service_ded = 0.0
    prev = 0
    for cap, base, per in p["retire_service_deduction"]:
        if n <= cap:
            service_ded = base + (n - prev) * per
            break
        prev = cap
    service_ded = min(service_ded, income)              # 공제는 퇴직소득금액을 넘지 않는다
    converted = (income - service_ded) * 12 / n
    conv_ded = 0.0
    prev_c = 0.0
    for cap, base, rate in p["retire_conv_deduction"]:
        if converted <= cap:
            conv_ded = base + (converted - prev_c) * rate
            break
        prev_c = cap
    tax_base = max(converted - conv_ded, 0.0)
    conv_tax = max(_progressive_tax(tax_base, p), 0.0)
    income_tax = _trunc10(conv_tax * n / 12)
    local_tax = _trunc10(income_tax * 0.1)
    return {"퇴직소득금액": round(income), "근속연수": n, "근속연수공제": round(service_ded),
            "환산급여": round(converted), "환산급여공제": round(conv_ded),
            "과세표준": round(tax_base), "환산산출세액": round(conv_tax),
            "소득세": income_tax, "지방소득세": local_tax, "합계": income_tax + local_tax}


def _earned_income_deduction(annual: float, p: dict) -> float:
    ded = 0.0
    prev = 0
    for cap, base, rate in p["earned_deduction"]:
        if annual <= cap:
            ded = base + (annual - prev) * rate
            break
        prev = cap
    # 근로소득공제 한도(소득세법 §47① 단서) — 미적용 시 총급여 3.6억+ 구간에서 공제 무한증가로
    # 과세표준 과소·원천징수 과소(적대검토 HIGH-A). 한도는 연도 파라미터.
    cap_amt = p.get("earned_deduction_cap")
    return min(ded, cap_amt) if cap_amt is not None else ded


def _progressive_tax(base: float, p: dict) -> float:
    for cap, rate, deduct in p["brackets"]:
        if base <= cap:
            return base * rate - deduct
    return 0


def _earned_income_tax_credit(computed_tax: float, annual_gross: float, p: dict) -> float:
    """근로소득세액공제(소득세법 §59) — 산출세액 55%/30% 2단 + 총급여별 한도.

    간이세액표의 핵심 공제. 종전 월 추정은 이를 누락해 과대 원천징수 → 연말정산 대량 환급 유발.
    """
    if computed_tax <= p["eitc_threshold"]:
        credit = computed_tax * p["eitc_rate_low"]
    else:
        credit = p["eitc_base"] + (computed_tax - p["eitc_threshold"]) * p["eitc_rate_high"]
    limit = 0.0
    prev_cap = 0.0
    for cap, base, decay, floor in p["eitc_limits"]:
        if annual_gross <= cap:
            limit = max(base - (annual_gross - prev_cap) * decay, floor)
            break
        prev_cap = cap
    return min(credit, limit)


def _child_tax_credit(children: int, p: dict) -> float:
    """자녀세액공제(연·8~20세) — 1명·2명·3명째부터 인당(TAX_PARAMS 연도값)."""
    if children <= 0:
        return 0
    if children == 1:
        return p["child_first"]
    if children == 2:
        return p["child_second"]
    return p["child_second"] + (children - 2) * p["child_extra"]


def estimate_monthly_income_tax(monthly_gross: float, *, dependents: int = 1,
                                children: int = 0, year: int | None = None) -> dict:
    """월 근로소득세 — 국세청 간이세액표 계산방법: 연환산→근로소득공제·인적공제→산출세액→
    **근로소득세액공제·자녀세액공제**→÷12. children=8~20세 자녀수(옵션). 지방소득세=소득세 10%.
    year 미지정=당해연도(TAX_PARAMS 미등록 시 예외 — 갱신 강제 fail-safe·H-1)."""
    p = _tax_params(year)
    annual = monthly_gross * 12
    taxable = max(annual - _earned_income_deduction(annual, p)
                  - dependents * p["personal_deduction"], 0)
    computed = max(_progressive_tax(taxable, p), 0)
    net = max(computed - _earned_income_tax_credit(computed, annual, p)
              - _child_tax_credit(children, p), 0)
    income_tax = _trunc10(net / 12)
    return {"소득세": income_tax, "지방소득세": _trunc10(income_tax * 0.1)}


def year_end_settlement(annual_salary: float, *, withheld_tax: float,
                        dependents: int = 1, children: int = 0,
                        year: int | None = None, pension_paid: float = 0,
                        insurance_paid: float = 0) -> dict:
    """연말정산(간이) — 근로소득공제·인적공제·연금보험료공제·보험료 특별소득공제·누진세율·근로소득세액공제·자녀세액공제.

    pension_paid=국민연금 근로자 납부액(연금보험료공제 전액) · insurance_paid=건강·장기요양·고용보험 근로자 납부액(특별소득공제 전액).
    둘 다 급여 공제 줄에 있는 값이라 ERP가 정확히 안다(v1.304 — 종전엔 빠져 결정세액이 실제보다 컸다). 의료비·교육비·기부금·카드·
    주택자금 등 증빙 공제는 미반영."""
    p = _tax_params(year)
    earned_deduction = _earned_income_deduction(annual_salary, p)
    income = annual_salary - earned_deduction              # 근로소득금액
    personal = p["personal_deduction"] * max(dependents, 1)  # 기본 인적공제
    def _determined(special: float, standard_credit: float) -> tuple[float, float, int]:
        base = max(income - personal - max(pension_paid, 0) - special, 0)   # 과세표준
        calc = _progressive_tax(base, p)                                     # 산출세액
        cr = _earned_income_tax_credit(calc, annual_salary, p) + _child_tax_credit(children, p) + standard_credit
        return base, calc, round(max(calc - cr, 0))
    # 보험료 특별소득공제를 받거나, 받지 않고 표준세액공제(13만·§59의4⑨)를 받거나 — 근로자에게 유리한 쪽(저소득자는 표준공제가 유리)
    tax_base, calculated, determined = _determined(max(insurance_paid, 0), 0)
    std = _determined(0, float(p.get("standard_credit", 130_000)))
    basis = "보험료 특별소득공제"
    if std[2] < determined:
        tax_base, calculated, determined = std
        basis = "표준세액공제(13만)"
    settlement = round(withheld_tax - determined)          # +환급 / −추가납부
    return {"총급여": annual_salary, "근로소득공제": round(earned_deduction),
            "연금보험료공제": round(max(pension_paid, 0)),
            "보험료공제": round(max(insurance_paid, 0)) if basis.startswith("보험료") else 0, "공제 방식": basis,
            "과세표준": round(tax_base), "산출세액": round(calculated),
            "결정세액": determined, "기납부세액": round(withheld_tax),
            "정산액": settlement, "환급여부": settlement >= 0}
