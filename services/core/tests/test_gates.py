"""Gate 구현 검증 — G2 간호등급·G5 법정 기한·G3 전공의 4주 평균 (2026-06-07 조사 기반)."""
from datetime import date

import pytest

from erp.claims.deadline import EXTRA_HOLIDAYS, appeal_deadline, is_holiday
from erp.hr.duty_service import DutyCell, DutyService
from erp.hr.models import Employee
from erp.regulatory.nursing_grade import (
    compute_grade,
    part_time_fte,
    quarter_window,
    round_avg,
    truncate_ratio,
)


# ── G2: 간호등급 (고시 제2023-235호) ────────────────────────
def test_quarter_window_q2():
    """6월(2분기) 산정 → 전전분기(4분기' 아님 — 작년 12월) 15일 ~ 전분기 말월(3월) 14일."""
    start, end = quarter_window(date(2026, 6, 7))
    assert start == date(2025, 12, 15) and end == date(2026, 3, 14)


def test_quarter_window_q1():
    start, end = quarter_window(date(2026, 1, 10))
    assert start == date(2025, 9, 15) and end == date(2025, 12, 14)


def test_rounding_rules_per_notice():
    """평균=셋째자리 반올림, 비율=셋째자리 이하 절사 (고시 라.4)."""
    assert round_avg(12.345) == 12.35 and round_avg(12.344) == 12.34
    assert truncate_ratio(2.4999) == 2.49  # 절사 — 반올림이면 2.50 (등급이 바뀜!)


def test_grade_band_boundary_tertiary():
    """상급종합 (확정 구간): 1.49 → S, 1.5 → 1등급 (미만 경계)."""
    start, end = date(2026, 1, 1), date(2026, 1, 10)  # 10일
    r1 = compute_grade(patient_days=149, nurse_days=100, period_start=start,
                       period_end=end, hospital_type="TERTIARY")
    assert r1.ratio == 1.49 and r1.grade == "S"
    r2 = compute_grade(patient_days=150, nurse_days=100, period_start=start,
                       period_end=end, hospital_type="TERTIARY")
    assert r2.ratio == 1.5 and r2.grade == "1"


def test_grade_truncation_protects_boundary():
    """절사 규칙이 등급 경계를 보호 — 2.499→2.49 (반올림이면 등급 하락)."""
    start, end = date(2026, 1, 1), date(2026, 1, 10)
    r = compute_grade(patient_days=249.9, nurse_days=100, period_start=start,
                      period_end=end, hospital_type="TERTIARY")
    assert r.ratio == 2.49 and r.grade == "2"


def test_grade_hospital_bands_confirmed():
    """병원급 확정 구간 (산정지침 마목 ㈐): 1.99→A, 2.0→1등급, 6.0→6등급."""
    start, end = date(2026, 1, 1), date(2026, 1, 10)
    a = compute_grade(patient_days=199, nurse_days=100, period_start=start, period_end=end)
    assert a.ratio == 1.99 and a.grade == "A"
    g1 = compute_grade(patient_days=200, nurse_days=100, period_start=start, period_end=end)
    assert g1.grade == "1"
    g6 = compute_grade(patient_days=600, nurse_days=100, period_start=start, period_end=end)
    assert g6.grade == "6"


def test_grade_general_hospital_bands():
    """종합병원 (마목 ㈏): S<1.5, A<2.0, 5등급 ≥6.0."""
    start, end = date(2026, 1, 1), date(2026, 1, 10)
    s = compute_grade(patient_days=140, nurse_days=100, period_start=start,
                      period_end=end, hospital_type="GENERAL")
    assert s.grade == "S"
    g5 = compute_grade(patient_days=700, nurse_days=100, period_start=start,
                       period_end=end, hospital_type="GENERAL")
    assert g5.grade == "5"


def test_grade_no_nurses_worst():
    r = compute_grade(patient_days=100, nurse_days=0,
                      period_start=date(2026, 1, 1), period_end=date(2026, 1, 10),
                      hospital_type="TERTIARY")
    assert r.grade == "3"


def test_part_time_fte_table():
    """단시간 환산 (고시 다.3)나) — 16h 미만 제외)."""
    assert part_time_fte(40) == 1.0
    assert part_time_fte(36) == 0.9 and part_time_fte(16) == 0.4
    assert part_time_fte(15.9) == 0.0


# ── G5: 이의신청 기한 (건보법 §87 + 민법 §157/§161) ────────
def test_deadline_plain_90_days():
    """평일 만료 — 도달일+90일 (초일불산입 반영)."""
    # 2026-01-05(월) 도달 → +90 = 2026-04-05(일) → §161 익일(월) 4/6
    assert appeal_deadline(date(2026, 1, 5)) == date(2026, 4, 6)


def test_deadline_weekend_rolls_forward():
    # 2026-01-07(수) → +90 = 4/7(화) 평일 그대로
    assert appeal_deadline(date(2026, 1, 7)) == date(2026, 4, 7)


def test_deadline_holiday_chain():
    """말일이 공휴일 연휴면 연쇄 익일 (민법 §161)."""
    # 10/3 개천절(토) → 10/4(일) → 10/5(대체공휴일, 2026 등록분) → 10/6(화)
    d = appeal_deadline(date(2026, 7, 5))
    assert d == date(2026, 10, 6)


def test_deadline_extra_holiday_table(monkeypatch):
    """연도별 추가 공휴일 테이블 반영 (주입 케이스)."""
    monkeypatch.setitem(EXTRA_HOLIDAYS, 2027, {(4, 7)})
    assert appeal_deadline(date(2027, 1, 7)) == date(2027, 4, 8)


def test_deadline_seollal_2026_registered():
    """2026 설 연휴(2/16~18 등록분) — 말일이 설날이면 연휴 종료 후 평일로."""
    # 도달 2025-11-18 → +90 = 2026-02-16(월·설 연휴) → 17·18 연휴 → 2/19(목)
    assert appeal_deadline(date(2025, 11, 18)) == date(2026, 2, 19)


def test_deadline_180_day_cap():
    """처분일 180일 상한 — 안 날 90일보다 이르면 상한 적용 (법 §87)."""
    arrived = date(2026, 6, 1)
    disposed = date(2026, 1, 2)  # +180 = 2026-07-01(수)
    assert appeal_deadline(arrived, disposed_on=disposed) == date(2026, 7, 1)


def test_is_holiday_fixed():
    assert is_holiday(date(2026, 5, 5))      # 어린이날
    assert is_holiday(date(2026, 6, 6))      # 현충일(토 — 둘 다 해당)
    assert not is_holiday(date(2026, 6, 4))  # 평일(목)


# ── G3: 전공의 4주 평균 80h (전공의법 §7①) ─────────────────
@pytest.mark.usefixtures("session")
async def test_resident_avg_rule(session):
    res = Employee(employee_no="R900", name="전공의", job_group="RESIDENT",
                   department_id=10, hired_on=date(2025, 3, 1))
    session.add(res)
    await session.flush()

    # 6월 1~28일 (4주): 주1 88h(11일×8h… 단순화 — 매일 D+E 불가하므로 셀당 8h)
    # 한 주 96h(매일 N 7일 + 추가 불가) 대신: 1~2주는 풀근무(56h), 3~4주 풀근무 → 평균 56h < 80 OK
    cells_ok = [DutyCell(employee_id=res.id, duty_on=date(2026, 6, d), shift="D")
                for d in range(1, 29)]
    svc = DutyService()
    _, violations = await svc.save_month(session, department_id=10, yyyymm="2026-06",
                                         cells=cells_ok)
    assert not any(v.rule == "RESIDENT_AVG_HOURS" for v in violations)
    # 주 평균이 80h를 넘는 케이스는 8h 셀 모델로는 주 56h 상한 — 위반 미발생이 정상.
    # (G3 잔여: 셀 모델이 콜당직(24h+)을 표현하게 되면 한계 검증 케이스 추가)
