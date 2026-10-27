"""고용형태별 4대보험·소득세 특례 (P2) — 일용 적용기준·일용 분리과세."""
from erp.hr import statutory


def test_regular_insurance_full():
    out = statutory.insurance(3_000_000, year=2026)
    assert out["국민연금"] > 0 and out["건강보험"] > 0 and out["고용보험"] > 0


def test_daily_below_threshold_excludes_pension_health():
    # 일용직 월 5일·40시간 → 국민연금·건강보험 적용제외(0), 고용보험만
    out = statutory.insurance(1_000_000, year=2026, employment_type="DAILY",
                              work_days=5, work_hours=40)
    assert out["국민연금"] == 0 and out["건강보험"] == 0 and out["장기요양"] == 0
    assert out["고용보험"] > 0


def test_daily_above_threshold_applies():
    # 월 8일↑ 또는 60h↑ → 가입
    out = statutory.insurance(2_000_000, year=2026, employment_type="DAILY",
                              work_days=10, work_hours=72)
    assert out["국민연금"] > 0 and out["건강보험"] > 0


def test_daily_hours_threshold_alone():
    out = statutory.insurance(2_000_000, year=2026, employment_type="DAILY",
                              work_days=4, work_hours=60)
    assert out["국민연금"] > 0          # 60h 단독 충족


def test_watch_and_contract_same_as_regular():
    base = statutory.insurance(3_000_000, year=2026)
    for et in ("WATCH", "CONTRACT", "TEMP"):
        assert statutory.insurance(3_000_000, year=2026, employment_type=et) == base


def test_daily_withholding_basic():
    # 일급 20만 → (200000-150000)×2.7% = 1,350원/일, 소액부징수 초과
    out = statutory.daily_withholding(200_000, days=10, year=2026)
    assert out["일소득세"] == 1_350 and out["소득세"] == 13_500
    assert out["지방소득세"] == 1_350 and out["합계"] == 14_850


def test_daily_withholding_under_deduction_zero():
    assert statutory.daily_withholding(150_000, days=5, year=2026)["소득세"] == 0   # 15만 이하 비과세


def test_daily_withholding_small_amount_exempt():
    # 일급 16만 → (160000-150000)×2.7%=270원 < 1,000 → 소액부징수 면제
    assert statutory.daily_withholding(160_000, days=3, year=2026)["소득세"] == 0
