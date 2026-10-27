"""직종×고용형태 급여 규칙엔진 (P3) — 명세 자동생성."""
from erp.hr import payroll_rules as pr


def _codes(result):
    return {ln["item_code"] for ln in result["lines"]}


def _amt(result, code):
    return next((ln["amount"] for ln in result["lines"] if ln["item_code"] == code), None)


def test_doctor_regular_with_duty():
    r = pr.build_payroll(staff_class="MEDICAL", job_category="DOCTOR",
                         employment_type="REGULAR", base_pay=5_000_000, duty_count=5)
    assert _amt(r, "BASE") == 5_000_000 and _amt(r, "DUTY") == 500_000
    assert r["gross"] == 5_500_000
    assert {"NP", "HI", "TAX"} <= _codes(r)            # 4대보험·소득세 공제
    assert r["net"] == r["gross"] - r["deduction_total"]


def test_nurse_night_allowance():
    r = pr.build_payroll(job_category="NURSE", base_pay=3_000_000, night_hours=40)
    assert _amt(r, "NIGHT") == 40 * 12_000
    assert "DUTY" not in _codes(r)                     # 간호사는 당직비 아님


def test_tech_qualification_allowance():
    r = pr.build_payroll(job_category="RADIOTECH", base_pay=2_800_000)
    assert _amt(r, "QUAL") == 150_000


def test_daily_security_worker():
    # 일용 경비: 일당 20만 × 20일 + 감시단속수당, 일용 4대보험(20일→가입)·일용 분리과세
    r = pr.build_payroll(job_category="SECURITY", employment_type="DAILY",
                         daily_wage=200_000, work_days=20, work_hours=160)
    assert _amt(r, "BASE") == 4_000_000 and _amt(r, "WATCH") == 100_000
    assert _amt(r, "NP") and _amt(r, "HI")             # 20일 → 적용기준 충족
    assert _amt(r, "TAX") == 1_350 * 20                # 일용 분리과세 (20만−15만)×2.7%×20일


def test_daily_below_threshold_no_pension():
    # 일용 5일 → 국민연금·건강 적용제외(라인 없음), 고용보험만
    r = pr.build_payroll(job_category="SECURITY", employment_type="DAILY",
                         daily_wage=150_000, work_days=5, work_hours=40)
    codes = _codes(r)
    assert "NP" not in codes and "HI" not in codes and "EI" in codes
    assert "TAX" not in codes                          # 일급 15만 이하 → 일용세 0


def test_watch_regular_allowance():
    r = pr.build_payroll(job_category="SECURITY", employment_type="WATCH", base_pay=2_500_000)
    assert _amt(r, "WATCH") == 100_000 and _amt(r, "NP")   # 감시단속은 상용 4대보험


def _basis(result, code):
    return next((ln.get("calc_basis") for ln in result["lines"] if ln["item_code"] == code), None)


def test_calc_basis_formula_on_variable_and_deduction_lines():
    """P1 — 근로시간 연동 항목·공제에 계산방법(formula·구조화) 부여, 월정액은 계산식 없음."""
    r = pr.build_payroll(job_category="DOCTOR", employment_type="REGULAR",
                         base_pay=4_180_000, duty_count=3, ot_hours=10)
    ot = _basis(r, "OT")
    assert ot and ot["hours"] == 10 and ot["rate"] == 1.5 and ot["hourly"] == 20_000
    assert "10h" in ot["formula"] and "1.5" in ot["formula"]        # "10h × 20,000 × 1.5"
    duty = _basis(r, "DUTY")
    assert duty["count"] == 3 and "3회" in duty["formula"]
    np = _basis(r, "NP")
    assert np and np["rate"] == 0.0475 and "4.75%" in np["formula"]   # 2026 고시 9.5%의 절반
    assert _basis(r, "BASE") is None                                # 월정액 계산식 없음
    tax = _basis(r, "TAX")
    assert tax and tax["method"] == "근사산식" and "간이세액표 미등록" in tax["formula"]   # 표 없을 때 표기를 사실대로(v1.297)
