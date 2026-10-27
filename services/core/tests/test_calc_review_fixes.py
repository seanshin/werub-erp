"""계산식 전수검토(2026-07-15) 결함 수정 회귀 — docs/05-reference/calculation-formulas.md §9."""
from datetime import date

from erp.app.compliance_service import ComplianceDeadlineService
from erp.finance.allowance_service import _DEFAULT_RATES, AllowanceService
from erp.finance.models import AllowanceRate
from erp.hr.models import Employee
from erp.regulatory.models import RadiationDose
from erp.scm.models import MedicalWasteLog


async def test_m2_allowance_rates_partial_seed_merges_defaults(session):
    """M-2 — 요율 부분 시드 시 누락 버킷이 0이 아니라 기본요율로 병합(대손 과소 방지)."""
    session.add(AllowanceRate(kind="AR", bucket="90+", rate=0.9))   # 1개 버킷만 시드
    await session.flush()
    rates = await AllowanceService().rates(session, kind="AR")
    assert rates["90+"] == 0.9                                  # 시드 오버라이드
    assert rates["current"] == _DEFAULT_RATES["current"]        # 누락 버킷 = 기본요율(0 아님)
    assert all(b in rates for b in _DEFAULT_RATES)


async def test_m6_isolation_waste_7day_deadline(session):
    """M-6 — 격리의료폐기물 보관기한 7일(종전 기본 15일 처리로 경고 8일 지각)."""
    session.add(MedicalWasteLog(waste_type="격리", weight_kg=1.0,
                                stored_on=date(2026, 7, 1), status="STORED"))
    await session.flush()
    out = await ComplianceDeadlineService().deadlines(
        session, as_of=date(2026, 7, 1), horizon_days=30)
    item = next(i for i in out["items"] if "격리" in i["title"])
    assert item["due_on"] == "2026-07-08"                       # 7/1 + 7일(15일 아님)


async def test_m7_radiation_5y_cumulative_limit(session):
    """M-7 — 5년 누적 100mSv 한도 경보(연 50mSv만 보던 갭)."""
    e = Employee(employee_no="R-1", name="김방사", hired_on=date(2020, 1, 1))
    session.add(e)
    await session.flush()
    # 연 30mSv × 4개년 = 누적 120 > 100 (각 연도는 연한도 50 미만 → 종전 미경보)
    for y in (2023, 2024, 2025, 2026):
        session.add(RadiationDose(employee_id=e.id, period=f"{y}-Q1", dose_msv=30,
                                  measured_on=date(y, 3, 1)))
    await session.flush()
    out = await ComplianceDeadlineService().deadlines(
        session, as_of=date(2026, 7, 1), horizon_days=30)
    assert not out["alerts"]["radiation_over_limit"]            # 연한도는 미초과
    over5 = out["alerts"]["radiation_over_5y_limit"]
    assert len(over5) == 1 and over5[0]["cum5_msv"] == 120.0


def test_m9_risk_amount_same_code_multi_dates():
    """M-9 — 위험액이 (코드,진료일) 키 — 동일 코드 상이 진료일은 합산·동일 키 다중룰은 max."""
    # worklist 내부 집계 로직과 동형의 순수 검증(서비스 호출 없이 규약 고정)
    findings = [
        {"code": "A100", "treated_on": date(2026, 6, 1), "amount": 1000.0},   # 라인1
        {"code": "A100", "treated_on": date(2026, 6, 2), "amount": 2000.0},   # 라인2(다른 날)
        {"code": "A100", "treated_on": date(2026, 6, 2), "amount": 1500.0},   # 라인2 중복룰(max)
        {"code": None, "treated_on": date(2026, 6, 3), "amount": 300.0},      # 무코드 개별
    ]
    by_key: dict[tuple, float] = {}
    no_code = 0.0
    for f in findings:
        if f["code"]:
            k = (f["code"], f["treated_on"])
            by_key[k] = max(by_key.get(k, 0.0), f["amount"])
        else:
            no_code += f["amount"]
    assert sum(by_key.values()) + no_code == 1000 + 2000 + 300  # 3300(종전 코드 단일키=1800)


def test_m8_surcharge_holiday_db_injection():
    """M-8 — 공휴가산 판정에 DB 캘린더(date 집합) 주입 시 코드 테이블에 없는 날도 공휴 인정."""
    from erp.claims.pre_review import _is_surcharge_holiday
    d = date(2027, 2, 8)   # 임의 평일(월) — 코드 EXTRA_HOLIDAYS(2026만)에 없음
    assert d.weekday() == 0
    assert not _is_surcharge_holiday(d)                          # 주입 없음 → 평일
    assert _is_surcharge_holiday(d, extra_holidays={d})          # DB 주입 → 공휴 인정


# ── H-1 소득세 파라미터 연도화 / H-2 휴일근로수당 ─────────────────────────────

def test_h1_tax_params_failsafe_unregistered_year():
    """H-1 — 미등록 연도는 ValidationError로 갱신 강제(RATES 대칭·무음 구식세액 차단)."""
    import pytest

    from erp.errors import ValidationError
    from erp.hr import statutory
    with pytest.raises(ValidationError, match="소득세 파라미터 미등록"):
        statutory.estimate_monthly_income_tax(3_000_000, year=2031)
    with pytest.raises(ValidationError):
        statutory.year_end_settlement(36_000_000, withheld_tax=1_200_000, year=2031)
    with pytest.raises(ValidationError):
        statutory.daily_withholding(200_000, year=2031)
    # 등록 연도는 종전과 동일 값(무회귀) — 월300만/부양1 = 133,450(v1.116 검증값)
    t = statutory.estimate_monthly_income_tax(3_000_000, dependents=1, year=2026)
    assert t["소득세"] == 133_450


def test_h2_build_payroll_holiday_line():
    """H-2 — 휴일근로수당: 8h이내 ×1.5 + 초과 ×2.0 (근기법 §56②③)."""
    from erp.hr.payroll_rules import STANDARD_MONTHLY_HOURS, build_payroll
    base = 2_090_000                       # 통상시급 = 10,000원
    hourly = base / STANDARD_MONTHLY_HOURS
    out = build_payroll(employment_type="REGULAR", base_pay=base,
                        holiday_hours=8, holiday_ot_hours=2, year=2026)
    line = next(e for e in out["lines"] if e["item_code"] == "HOLIDAY")
    assert line["amount"] == int(round(8 * hourly * 1.5 + 2 * hourly * 2.0))  # 160,000
    # 일용·감시단속은 제외(일당·포괄 정산)
    out2 = build_payroll(employment_type="DAILY", daily_wage=100_000, work_days=5,
                         holiday_hours=8, year=2026)
    assert not any(e["item_code"] == "HOLIDAY" for e in out2["lines"])


async def test_h2_pay_inputs_holiday_from_staging(session):
    """H-2 — 일요일 스테이징 근무가 휴일시간으로 분리되고 평일 연장에서 제외(이중지급 방지)."""
    from datetime import datetime

    from erp.hr.models import AttendanceSummary, Employee, GroupwareAttendanceStaging
    from erp.hr.pay_inputs import resolve_monthly_inputs
    emp = Employee(employee_no="H2-1", name="휴일근무", hired_on=date(2025, 1, 1))
    session.add(emp)
    await session.flush()
    # 2026-06-07(일) 10시간 근무 → 휴일 8h(×1.5) + 초과 2h(×2.0)
    session.add(GroupwareAttendanceStaging(
        source_ref="his:H2-1:2026-06-07", external_user_id="x", employee_no="H2-1",
        work_on=date(2026, 6, 7), clock_in=datetime(2026, 6, 7, 8, 0),
        clock_out=datetime(2026, 6, 7, 18, 0), status="APPLIED"))
    # 월 집계: 총 21일·178h (평일 20일×8h=160h + 휴일 10h + 평일연장 8h)
    session.add(AttendanceSummary(employee_id=emp.id, yyyymm="2026-06",
                                  work_days=21, total_work_minutes=178 * 60))
    await session.flush()
    out = await resolve_monthly_inputs(session, emp, "2026-06")
    assert out["holiday_hours"] == 8.0 and out["holiday_ot_hours"] == 2.0
    # 평일 연장 = 178 − 10(휴일) − 8×20(휴일일 제외 근무일) = 8h — 휴일분 미중복
    assert out["ot_hours"] == 8.0


async def test_governance_pay_rate_periodization(session):
    """거버넌스 — 수당 단가 기간 이력: 급여월 기준 유효 단가(소급 왜곡 방지)·이력 로그."""
    from erp.hr.pay_rates import load_rates, rate_history, upsert_rate
    await upsert_rate(session, actor=1, rate_key="DUTY_PAY", amount=100_000,
                      valid_from=date(2026, 1, 1))
    await upsert_rate(session, actor=1, rate_key="DUTY_PAY", amount=120_000,
                      valid_from=date(2026, 7, 1), description="2026 단협 개정")
    # 6월 급여 = 구 단가(소급 왜곡 없음), 7월부터 신 단가
    assert (await load_rates(session, as_of=date(2026, 6, 1)))["DUTY_PAY"] == 100_000
    assert (await load_rates(session, as_of=date(2026, 7, 1)))["DUTY_PAY"] == 120_000
    # 이력 = 불변 로그(발효일 내림차순)
    hist = await rate_history(session, rate_key="DUTY_PAY")
    assert [h["amount"] for h in hist] == [120_000, 100_000]
    assert hist[0]["description"] == "2026 단협 개정"


def test_governance_next_year_param_deadline_source():
    """거버넌스 — 내년 요율·세법 파라미터 미등재 시 연말 만기 항목(사전 경보)."""
    from erp.hr.statutory import RATES, TAX_PARAMS
    # 2026 시점 2027 미등재 → 연말(12/31)이 horizon에 들면 경보 대상(구성 검증)
    assert 2027 not in RATES and 2027 not in TAX_PARAMS


def test_m11_night_pay_modes():
    """M-11 — 야간수당 산정 방식: FLAT(정액·현행)/STATUTORY(통상시급×0.5)/MAX(큰 값)."""
    from erp.hr.payroll_rules import build_payroll

    def night_amt(base, mode):
        out = build_payroll(job_category="NURSE", employment_type="REGULAR",
                            base_pay=base, night_hours=10, year=2026, night_pay_mode=mode)
        return next(e for e in out["lines"] if e["item_code"] == "NIGHT")["amount"]

    base = 2_090_000                       # 통상시급 1만 → 법정 0.5가산 = 5,000/h < 정액 12,000/h
    assert night_amt(base, "FLAT") == 120_000
    assert night_amt(base, "STATUTORY") == 50_000
    assert night_amt(base, "MAX") == 120_000               # 정액이 큼
    hi = 6_270_000                          # 통상시급 3만 → 법정 15,000/h > 정액 12,000/h
    assert night_amt(hi, "MAX") == 150_000                 # 법정이 큼(미달 방지)
    assert night_amt(hi, "FLAT") == 120_000                # 현행(기본) 무회귀


async def test_m11_settings_mode_persist(session):
    """M-11 — 설정 저장·effective 반영(기본 FLAT·검증)."""
    import pytest

    from erp.errors import ValidationError
    from erp.hr.payroll_settings import PayrollSettingsService
    svc = PayrollSettingsService()
    assert (await svc.effective(session))["night_pay_mode"] == "FLAT"   # 미설정 기본
    await svc.upsert(session, actor=1, incentive_withholding_method="NONE",
                     night_pay_mode="MAX")
    assert (await svc.effective(session))["night_pay_mode"] == "MAX"
    with pytest.raises(ValidationError):
        await svc.upsert(session, actor=1, incentive_withholding_method="NONE",
                         night_pay_mode="BOGUS")


# ── 회계연도 설정 (org_settings.fiscal_year_start_month) ─────────────────────

def test_fiscal_range_pure():
    """FY 범위 — 시작월 1=역년(무회귀)·시작월 3=3/1~익년 2월 말(윤년 안전)."""
    from erp.finance.fiscal import fiscal_range_for
    assert fiscal_range_for(1, 2026) == (date(2026, 1, 1), date(2026, 12, 31))
    assert fiscal_range_for(3, 2026) == (date(2026, 3, 1), date(2027, 2, 28))
    assert fiscal_range_for(3, 2027) == (date(2027, 3, 1), date(2028, 2, 29))  # 윤년 말일
    assert fiscal_range_for(7, 2026) == (date(2026, 7, 1), date(2027, 6, 30))


async def test_fiscal_year_setting_applies_to_closing(session):
    """기관 시작월 설정 → 연말결산이 회계연도 창으로 손익 집계(창 밖 전표 제외)."""
    from erp.common.org_service import OrgSettingsService
    from erp.finance.closing_service import ClosingService
    from erp.finance.fiscal import fiscal_range
    from erp.finance.models import Account, Voucher, VoucherLine
    from erp.finance.service import VoucherLineDTO, VoucherService
    from sqlalchemy import select

    await OrgSettingsService().upsert(session, actor=1, name="테스트병원",
                                      fiscal_year_start_month=3)
    assert await fiscal_range(session, 2026) == (date(2026, 3, 1), date(2027, 2, 28))
    for code, name, kind in [("1110", "현금", "ASSET"), ("4110", "외래수익", "REVENUE"),
                             ("3300", "이익잉여금", "CAPITAL")]:
        session.add(Account(code=code, name=name, kind=kind))
    await session.flush()
    vs = VoucherService()
    # FY2026 창 안(2026-04)·창 밖(2026-02 — FY2025 귀속) 수익
    await vs.create(session, source_module="t", source_ref="in-fy", voucher_on=date(2026, 4, 1),
                    lines=[VoucherLineDTO("1110", "DEBIT", 1000),
                           VoucherLineDTO("4110", "CREDIT", 1000)])
    await vs.create(session, source_module="t", source_ref="out-fy", voucher_on=date(2026, 2, 1),
                    lines=[VoucherLineDTO("1110", "DEBIT", 500),
                           VoucherLineDTO("4110", "CREDIT", 500)])
    out = await ClosingService().close_year(session, fiscal_year=2026, user_id=1)
    assert out["net_income"] == 1000                      # 창 안만 집계(창 밖 500 제외)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "closing:2026"))
    assert v.voucher_on == date(2027, 2, 28)              # 결산일 = 회계연도 말일
    ln = (await session.scalars(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.account_code == "3300"))).one()
    assert float(ln.amount) == 1000


async def test_fiscal_setting_validation(session):
    from erp.common.org_service import OrgSettingsService
    import pytest

    from erp.errors import ValidationError
    with pytest.raises(ValidationError, match="1~12"):
        await OrgSettingsService().upsert(session, actor=1, name="병원",
                                          fiscal_year_start_month=13)


def test_ha_earned_income_deduction_cap():
    """H-A — 근로소득공제 2,000만원 한도(소득세법 §47① 단서)·고연봉 과소징수 해소."""
    from erp.hr import statutory

    # 총급여 5억 — 한도 없으면 공제 22,750,000, 한도 적용 시 20,000,000
    ys = statutory.year_end_settlement(500_000_000, withheld_tax=0, year=2026)
    assert ys["근로소득공제"] == 20_000_000
    # 한도 반영 결정세액(적대검토 검증값 165,260,000) − 표준세액공제 13만(v1.304 — 특별공제 없이 계산하면 §59의4⑨ 적용)
    assert ys["결정세액"] == 165_130_000 and ys["공제 방식"] == "표준세액공제(13만)"
    # 한도 미만 구간은 무영향(월 300만·부양1 간이세액 회귀 없음)
    assert statutory.year_end_settlement(40_000_000, withheld_tax=0, year=2026)["근로소득공제"] \
        == 7_500_000 + (40_000_000 - 15_000_000) * 0.15


def test_ma_daily_withholding_small_tax_by_total():
    """M-A — 일용 소액부징수는 지급 세액 '합계' 기준(일 단위 판정 시 전액 과소징수 해소)."""
    from erp.hr import statutory

    # 일급 18만 × 20일 일괄지급 — 일세액 810(<1,000)이나 합계 16,200 ≥ 1,000 → 징수
    out = statutory.daily_withholding(180_000, days=20, year=2026)
    assert out["소득세"] == 16_200 and out["지방소득세"] == 1_620
    # 단일 소액 지급(합계<1,000)은 여전히 부징수
    assert statutory.daily_withholding(155_000, days=1, year=2026)["소득세"] == 0


def test_mb_year_end_dependents_affect_determined_tax():
    """M-B — 부양가족 수가 결정세액에 반영(ESS 기본 1 고정 시 과대 '추가납부' 해소 근거)."""
    from erp.hr import statutory

    one = statutory.year_end_settlement(60_000_000, withheld_tax=0, dependents=1, year=2026)
    four = statutory.year_end_settlement(60_000_000, withheld_tax=0, dependents=4, year=2026)
    assert four["결정세액"] < one["결정세액"]                 # 부양 많을수록 인적공제↑ → 세액↓


async def test_hb_saturday_ot_by_roster(session):
    """H-B — 미배정 토요일 근로는 연장근로(주40h 근로자)·듀티 배정 토요일은 소정근로(교대제)."""
    from datetime import datetime

    from erp.hr.models import (
        AttendanceSummary, DutyAssignment, DutySchedule, Employee,
        GroupwareAttendanceStaging,
    )
    from erp.hr.pay_inputs import resolve_monthly_inputs

    office = Employee(employee_no="SAT-1", name="사무직", hired_on=date(2025, 1, 1))
    session.add(office)
    await session.flush()
    # 2026-07-04(토) 8h 근무 — 듀티 미배정 → 전부 연장
    session.add(GroupwareAttendanceStaging(
        source_ref="his:SAT-1:2026-07-04", external_user_id="x", employee_no="SAT-1",
        work_on=date(2026, 7, 4), clock_in=datetime(2026, 7, 4, 9, 0),
        clock_out=datetime(2026, 7, 4, 17, 0), status="APPLIED"))
    # 월 집계: 평일 22일×8h(176h) + 토요일 8h = 184h, 23일
    session.add(AttendanceSummary(employee_id=office.id, yyyymm="2026-07",
                                  work_days=23, total_work_minutes=184 * 60))
    await session.flush()
    out = await resolve_monthly_inputs(session, office, "2026-07")
    assert out["ot_hours"] == 8.0                            # 종전 버그: 0(토요일 소멸)

    # 교대제 간호사 — 같은 토요일이 듀티 배정(소정근로일) → 흡수(연장 아님)
    nurse = Employee(employee_no="SAT-2", name="교대간호", hired_on=date(2025, 1, 1),
                     job_group="NURSE", job_category="NURSE", department_id=1)
    session.add(nurse)
    await session.flush()
    sched = DutySchedule(department_id=1, yyyymm="2026-07", status="PUBLISHED")
    session.add(sched)
    await session.flush()
    session.add_all([
        DutyAssignment(schedule_id=sched.id, employee_id=nurse.id,
                       duty_on=date(2026, 7, 4), shift="D"),
        GroupwareAttendanceStaging(
            source_ref="his:SAT-2:2026-07-04", external_user_id="y", employee_no="SAT-2",
            work_on=date(2026, 7, 4), clock_in=datetime(2026, 7, 4, 9, 0),
            clock_out=datetime(2026, 7, 4, 17, 0), status="APPLIED"),
        AttendanceSummary(employee_id=nurse.id, yyyymm="2026-07",
                          work_days=23, total_work_minutes=184 * 60)])
    await session.flush()
    out2 = await resolve_monthly_inputs(session, nurse, "2026-07")
    assert out2["ot_hours"] == 0.0                           # 배정 토요일 = 소정근로 흡수


async def test_l4_holiday_missing_clockout_not_absorbed_into_baseline(session):
    """L-4 — 휴일 근무에 clock_out 누락 시 그 날을 휴일 근무일로 계상(평일 기준 흡수 방지)."""
    from datetime import datetime

    from erp.hr.models import AttendanceSummary, Employee, GroupwareAttendanceStaging
    from erp.hr.pay_inputs import resolve_monthly_inputs
    emp = Employee(employee_no="L4-1", name="퇴근누락", hired_on=date(2025, 1, 1))
    session.add(emp)
    await session.flush()
    # 2026-06-07(일) 출근만 기록·퇴근 누락
    session.add(GroupwareAttendanceStaging(
        source_ref="his:L4-1:2026-06-07", external_user_id="x", employee_no="L4-1",
        work_on=date(2026, 6, 7), clock_in=datetime(2026, 6, 7, 8, 0),
        clock_out=None, status="APPLIED"))
    # 월 집계: 21일·168h(평일 20일×8h=160h + 휴일 8h)
    session.add(AttendanceSummary(employee_id=emp.id, yyyymm="2026-06",
                                  work_days=21, total_work_minutes=168 * 60))
    await session.flush()
    out = await resolve_monthly_inputs(session, emp, "2026-06")
    # 휴일 시간 미상 → 가산액 0이나, 그 날이 평일 기준(8h×근무일)에서 제외돼 연장 8h로 산정
    assert out["holiday_hours"] == 0.0 and out["holiday_ot_hours"] == 0.0
    assert out["ot_hours"] == 8.0        # 종전 버그: 0(평일 기준 흡수)
