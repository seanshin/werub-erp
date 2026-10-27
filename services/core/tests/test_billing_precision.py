"""기관 종별·산정특례·의료급여종별 본인부담 정밀화 + 계산 메타 (D-6 P1)."""
from datetime import date

from erp.patient.billing_engine import compute_invoice, split_line


def test_grade_outpatient_rate():
    # 병원 40%(기본) vs 상급 60%(주입)
    base = split_line(total=10_000, coverage="NHIS", encounter_type="OUTPATIENT")
    assert base["patient"] == 4_000 and base["rate"] == 0.40
    tertiary = split_line(total=10_000, coverage="NHIS", encounter_type="OUTPATIENT",
                          grade="TERTIARY", rates={"OUTPATIENT": 0.60})
    assert tertiary["patient"] == 6_000
    assert "상급종합" in tertiary["rate_basis"]


def test_special_case_type_rates():
    cancer = split_line(total=100_000, coverage="NHIS", special_case_type="CANCER")
    assert cancer["patient"] == 5_000 and "산정특례(CANCER)" in cancer["rate_basis"]
    rare = split_line(total=100_000, coverage="NHIS", special_case_type="RARE")
    assert rare["patient"] == 10_000           # 희귀난치 10%
    tb = split_line(total=100_000, coverage="NHIS", special_case_type="TUBERCULOSIS")
    assert tb["patient"] == 0                   # 결핵 0%


def test_medical_aid_class():
    c1 = split_line(total=50_000, coverage="MEDICAL_AID", medical_aid_class="1")
    assert c1["patient"] == 0 and "1종" in c1["rate_basis"]
    c2 = split_line(total=50_000, coverage="MEDICAL_AID", medical_aid_class="2",
                    rates={"MEDICAL_AID_2": 0.15})
    assert c2["patient"] == 7_500


def test_inpatient_and_meal():
    ip = split_line(total=100_000, coverage="NHIS", encounter_type="INPATIENT")
    assert ip["patient"] == 20_000 and ip["rate_basis"] == "입원"
    meal = split_line(total=10_000, coverage="NHIS", encounter_type="INPATIENT",
                      is_meal=True)
    assert meal["patient"] == 5_000 and meal["rate_basis"] == "입원 식대"


def test_noncovered_and_no_fault():
    nc = split_line(total=30_000, coverage="NONCOVERED")
    assert nc["patient"] == 30_000 and nc["non_covered"] == 30_000
    auto = split_line(total=30_000, coverage="AUTO")
    assert auto["patient"] == 0 and "자동차보험" in auto["rate_basis"]


def test_compute_invoice_formula_metadata():
    lines = [{"code": "AA257", "name": "야간가산", "count": 3, "unit_price": 85_000,
              "coverage": "NHIS", "encounter_type": "OUTPATIENT", "code_type": "TARIFF"}]
    inv = compute_invoice(lines, grade="HOSPITAL")
    ln = inv["lines"][0]
    assert ln["formula"] == "85,000 × 3 = 255,000 → ×40% 본인부담"
    assert ln["patient"] == 102_000 and ln["code_type"] == "TARIFF"
    assert inv["patient_share"] == 102_000


async def test_load_billing_rates_seed(session):
    from erp.patient.billing_rates import load_billing_rates
    from erp.regulatory.models import InstitutionGradeSetting
    session.add(InstitutionGradeSetting(grade="HOSPITAL", outpatient_rate=0.40,
                                        inpatient_rate=0.20, meal_rate=0.50,
                                        special_case_rate=0.05,
                                        effective_from=date(2026, 1, 1)))
    # 종별 변경: 2026-07부터 종합병원 50%
    session.add(InstitutionGradeSetting(grade="GENERAL", outpatient_rate=0.50,
                                        inpatient_rate=0.20, meal_rate=0.50,
                                        special_case_rate=0.05,
                                        effective_from=date(2026, 7, 1)))
    await session.flush()
    may = await load_billing_rates(session, date(2026, 5, 15))
    assert may["grade"] == "HOSPITAL" and may["rates"]["OUTPATIENT"] == 0.40
    jul = await load_billing_rates(session, date(2026, 7, 10))
    assert jul["grade"] == "GENERAL" and jul["rates"]["OUTPATIENT"] == 0.50   # 시점관리


def test_benefit_classification():
    from erp.patient.billing_classify import classify_benefit
    assert classify_benefit("TARIFF", "재진진찰료-상급종합병원") == "CONSULT"
    assert classify_benefit("TARIFF", "전문병원 관리료-입원관리료") == "ADMISSION"
    assert classify_benefit("TARIFF", "일반식 입원환자 식대") == "MEAL"
    assert classify_benefit("TARIFF", "철대사검사-훼리틴") == "ACT"      # 행위료
    assert classify_benefit("DRUG", "아무약") == "DRUG"
    assert classify_benefit("MATERIAL", "거즈") == "MATERIAL"
    # 비급여 4분류
    assert classify_benefit("NONCOVERED", "상급병실료 1인실") == "ROOM"
    assert classify_benefit("NONCOVERED", "선택진료비") == "SELECT"
    assert classify_benefit("NONCOVERED", "일반진단서") == "CERTIFICATE"
    assert classify_benefit("NONCOVERED", "종합건강검진") == "SPECIAL_EXAM"


def test_patriot_coverage():
    from erp.patient.billing_engine import split_line
    p = split_line(total=50_000, coverage="PATRIOT")
    assert p["patient"] == 0 and "보훈" in p["rate_basis"]


def test_time_surcharge():
    from datetime import datetime
    from erp.patient.billing_classify import time_surcharge
    assert time_surcharge(datetime(2026, 6, 12, 22, 0)) == "야간"      # 금요일 22시
    assert time_surcharge(datetime(2026, 6, 14, 10, 0)) == "공휴"      # 일요일 10시
    assert time_surcharge(datetime(2026, 6, 14, 21, 0)) == "야간·공휴"  # 일요일 21시
    assert time_surcharge(datetime(2026, 6, 12, 14, 0)) is None       # 평일 주간


async def test_patriot_coverage_line_insert(session):
    """C1: 보훈 coverage 라인 인입 가능(CHECK 확장)·본인부담 면제."""
    from datetime import date
    from erp.patient.models import BillingLine
    session.add(BillingLine(line_id="LP", chart_no="C-P", code="AA", code_type="TARIFF",
                            count=1, coverage="PATRIOT", treated_on=date(2026, 6, 1)))
    await session.flush()    # CHECK 위반 없어야
    from erp.patient.billing_engine import split_line
    p = split_line(total=50_000, coverage="PATRIOT")
    assert p["patient"] == 0


def test_his_copayrate_sot_special_cases():
    """his_copay_rate SoT — 자격특수(상실·의료급여·특례)는 HIS, 일반은 ERP 자체계산."""
    from erp.patient.billing_engine import split_line
    # 자격상실 → HIS copayRate 1.0(전액)
    exp = split_line(total=10_000, coverage="NHIS", his_copay_rate=1.0,
                     eligibility_status="EXPIRED")
    assert exp["patient"] == 10_000 and "자격상실" in exp["rate_basis"]
    # 의료급여 1종 정액(HIS) → HIS copayRate 우선(ERP 0% 분기 대신)
    ma = split_line(total=10_000, coverage="MEDICAL_AID", medical_aid_class="1",
                    his_copay_rate=0.05)
    assert ma["patient"] == 500 and "의료급여 1종" in ma["rate_basis"]
    # 산정특례 → HIS copayRate
    sc = split_line(total=100_000, coverage="NHIS", special_case_type="CANCER",
                    his_copay_rate=0.05)
    assert sc["patient"] == 5_000 and "산정특례" in sc["rate_basis"]
    # 일반 급여(특수신호 없음) → ERP 자체계산(외래 40%), HIS copayRate 무시
    gen = split_line(total=10_000, coverage="NHIS", his_copay_rate=0.99)
    assert gen["patient"] == 4_000   # ERP 외래 40%, HIS 0.99 무시


def test_near_poor_copay_category():
    """차상위(NEAR_POOR) copay_category → HIS copayRate 신뢰(0.14)."""
    from erp.patient.billing_engine import split_line
    np = split_line(total=10_000, coverage="NHIS", his_copay_rate=0.14,
                    copay_category="NEAR_POOR")
    assert np["patient"] == 1_400 and "NEAR_POOR" in np["rate_basis"]
    # copay_category 없는 일반 → ERP 자체계산(외래 40%)
    gen = split_line(total=10_000, coverage="NHIS", his_copay_rate=0.14)
    assert gen["patient"] == 4_000
