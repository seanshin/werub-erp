"""급여 법정처리 (FR-07 확장) — 4대보험·퇴직금·연말정산 계산 검증."""
from datetime import date

from erp.hr import statutory


def test_insurance_standard():
    """보수월액 3,000,000 (2026 고시) — 근로자 부담 4대보험.

    2026-10-10: 종전 기대값(4.5%·3.545%·12.95%)은 2025 요율이었다 — 코드의 2026이 2025 복사본이라 테스트도
    그 값을 굳혀 버렸다. 출처: 복지부 보도자료(건강 7.19%·장기요양 0.9448%)·연금개혁(2026 9.5%).
    """
    r = statutory.insurance(3_000_000, year=2026)
    assert r["국민연금"] == 142_500          # 3,000,000 × 4.75%
    assert r["건강보험"] == 107_850          # × 3.595%
    assert r["장기요양"] == 14_170           # 107,850 × 13.14% = 14,171.49 → 10원 절사
    assert r["고용보험"] == 27_000           # × 0.9%
    assert r["합계"] == 142_500 + 107_850 + 14_170 + 27_000


def test_2026_rates_are_not_a_copy_of_2025():
    """해가 바뀌어 요율을 등록할 때 전년 복사로 끝내지 않았는지 — 2026은 연금·건강·장기요양 모두 인상."""
    a, b = statutory.RATES[2025], statutory.RATES[2026]
    for k in ("pension", "health", "longterm_of_health"):
        assert b[k] > a[k], k


def test_pension_upper_cap():
    """고액 급여 — 국민연금 상한(기준소득월액) 적용. 상·하한은 7월분부터 바뀐다."""
    assert statutory.insurance(7_000_000, year=2026)["국민연금"] == 302_570          # 6,370,000 × 4.75%
    assert statutory.insurance(7_000_000, year=2026, month=6)["국민연금"] == 302_570
    assert statutory.insurance(7_000_000, year=2026, month=7)["국민연금"] == 313_020  # 6,590,000 × 4.75%
    assert statutory.insurance(300_000, year=2026, month=1)["국민연금"] == 19_000     # 하한 400,000
    assert statutory.insurance(300_000, year=2026, month=12)["국민연금"] == 19_470    # 하한 410,000
    b = statutory.insurance(7_000_000, year=2026, month=7, with_basis=True)["_basis"]["NP"]
    assert b["base"] == 6_590_000


def test_insurance_zero_or_negative_gross():
    """심층 재검토 — 전월 무급휴가 등 보수월액 0/음수: 보험료 미부과(음수·하한 오부과 방지)."""
    for gross in (0, -50_000):
        r = statutory.insurance(gross, year=2026, with_basis=True)
        assert r["국민연금"] == 0 and r["건강보험"] == 0
        assert r["장기요양"] == 0 and r["고용보험"] == 0 and r["합계"] == 0
        assert r["_basis"]["NP"]["base"] == 0   # 하한(pension_min) 오부과 없음


def test_severance_eligible():
    s = statutory.severance(3_000_000, hire_date=date(2020, 1, 1), as_of=date(2026, 1, 1))
    assert s["지급대상"] is True
    assert s["근속일수"] == 2193             # 양끝 포함(as_of=마지막 재직일·시나리오 3회차)
    assert s["퇴직금"] == round(3_000_000 * 2193 / 365)


def test_severance_exact_one_year_inclusive_is_eligible():
    """입사 2025-10-01·마지막 재직일 2026-09-30 = 정확히 1년(365일) → 지급대상(종전 364일로 탈락)."""
    s = statutory.severance(3_000_000, hire_date=date(2025, 10, 1), as_of=date(2026, 9, 30))
    assert s["근속일수"] == 365 and s["지급대상"] is True
    assert statutory.severance(3_000_000, hire_date=date(2025, 10, 1),
                               as_of=date(2026, 9, 29))["지급대상"] is False


def test_severance_under_one_year():
    s = statutory.severance(3_000_000, hire_date=date(2025, 7, 1), as_of=date(2026, 1, 1))
    assert s["지급대상"] is False and s["퇴직금"] == 0


def test_year_end_additional_payment():
    """연봉 3,600만, 기납부 120만, 부양 1 → 추가납부."""
    y = statutory.year_end_settlement(36_000_000, withheld_tax=1_200_000, dependents=1,
                                      year=2026)
    assert y["근로소득공제"] == 10_650_000
    assert y["과세표준"] == 23_850_000
    # 근로소득세액공제 §59 정식(716,000 = 산출 2,317,500 초과분 30%·총급여 3,600만 한도) → 1,601,500
    # − 표준세액공제 13만(v1.304 — 보험료 공제 없이 계산하면 §59의4⑨) → 결정 1,471,500
    assert y["결정세액"] == 1_471_500 and y["공제 방식"] == "표준세액공제(13만)"
    assert y["정산액"] == -271_500 and y["환급여부"] is False


def test_year_end_refund():
    """기납부세액이 결정세액보다 크면 환급."""
    y = statutory.year_end_settlement(36_000_000, withheld_tax=2_000_000, dependents=2,
                                      year=2026)
    assert y["정산액"] > 0 and y["환급여부"] is True


def test_insurance_unregistered_year_fails_safe():
    """P1-2 — 미등록 연도(미래)는 무음폴백 대신 명시적 에러(잘못된 요율 방지)."""
    import pytest

    from erp.errors import ValidationError
    from erp.hr.statutory import insurance
    insurance(3_000_000, year=2026)              # 등록 연도 정상
    with pytest.raises(ValidationError, match="요율 미등록"):
        insurance(3_000_000, year=2099)          # 미등록 — fail-safe


def test_current_year_rates_are_registered():
    """당해연도 요율·소득세 파라미터가 등록돼 있어야 한다 — 해가 바뀌면 급여가 멈춘다.

    나머지 세액 단언은 `year=2026`(고시값)으로 못박아 달력과 무관하게 참이다. 그 대신
    '올해 것이 등록돼 있는가'는 **여기 한 곳에서만** 지킨다. 연초에 이 테스트만 빨개지면
    조치는 하나다 — `hr/statutory.py`의 `RATES`·`TAX_PARAMS`에 그 해 고시값을 추가.
    ⚠ 이 실패는 테스트 결함이 아니라 미등록 그 자체다(운영에서도 원천징수가 막힌다).
    """
    from datetime import date

    from erp.hr.statutory import RATES, TAX_PARAMS
    y = date.today().year
    assert y in RATES, f"{y}년 4대보험 요율 미등록 — RATES에 고시 요율 추가 (등록: {sorted(RATES)})"
    assert y in TAX_PARAMS, \
        f"{y}년 소득세 파라미터 미등록 — TAX_PARAMS에 고시 값 추가 (등록: {sorted(TAX_PARAMS)})"


def test_bonus_income_tax_prepaid_deduction():
    """상여세액(시행령§194) — 기납부세액(prepaid) 차감이 클수록 상여세액 감소(정식 C항)."""
    from erp.hr import statutory
    hi = statutory.bonus_income_tax(4_000_000, 1_000_000, months=1, prepaid_monthly_tax=0,
                                    year=2026)
    lo = statutory.bonus_income_tax(4_000_000, 1_000_000, months=1, prepaid_monthly_tax=300_000,
                                    year=2026)
    assert hi["소득세"] >= lo["소득세"] >= 0                 # 기납부 클수록 상여세액 작음
    assert lo["지방소득세"] == statutory._trunc10(lo["소득세"] * 0.1)  # 지방세=소득세10%


def test_employer_burden_components():
    """사업주 4대보험 부담 — 국민연금·건강·장기요양 근로자 동률 + 고용보험/산재 요율."""
    from erp.hr import statutory
    b = statutory.employer_burden(3_000_000, year=2026, ei_rate=0.0115, wc_rate=0.007)
    ee = statutory.insurance(3_000_000, year=2026)
    assert b["국민연금"] == ee["국민연금"] and b["건강보험"] == ee["건강보험"]   # 동률
    assert b["고용보험"] == statutory._trunc10(3_000_000 * 0.0115)
    assert b["산재"] == statutory._trunc10(3_000_000 * 0.007)
    assert b["합계"] == b["국민연금"] + b["건강보험"] + b["장기요양"] + b["고용보험"] + b["산재"]


def test_monthly_income_tax_applies_earned_credit():
    """월 근로소득세 — 근로소득세액공제(§59) 반영(종전 누락 대비 감소)."""
    r = statutory.estimate_monthly_income_tax(3_000_000, dependents=1, year=2026)  # 월300만·부양1
    # 산출 2,317,500 − 크레딧 716,000 = 1,601,500 → ÷12 절사 = 133,450 (종전 크레딧 누락 시 193,120)
    assert r["소득세"] == 133_450
    assert r["지방소득세"] == statutory._trunc10(r["소득세"] * 0.1)
    # 자녀세액공제 반영 시 추가 감소
    r2 = statutory.estimate_monthly_income_tax(3_000_000, dependents=3, children=2, year=2026)
    assert 0 <= r2["소득세"] < r["소득세"]


def test_child_tax_credit_2025_amendment_amounts():
    """자녀세액공제 2025 귀속분부터 첫째 25만·둘째 30만·셋째 이후 40만(누적 25/55/95만) — 종전 코드는 2024 값(15/35/65만)."""
    base = statutory.year_end_settlement(60_000_000, withheld_tax=0, dependents=4, year=2026)["결정세액"]
    for kids, credit in ((1, 250_000), (2, 550_000), (3, 950_000), (4, 1_350_000)):
        y = statutory.year_end_settlement(60_000_000, withheld_tax=0, dependents=4, children=kids, year=2026)
        assert base - y["결정세액"] == credit, kids
    assert statutory.TAX_PARAMS[2025]["child_first"] == 250_000
