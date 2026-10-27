"""간호관리료 차등제 산정 엔진 (FR-36, G2) — 고시 제2023-235호 + 2024 환자수 개편.

확정 규칙 (고시·복지부 질의응답 원문 — references/hira-edi-spec/):
- 산정 기간: **전전분기 마지막 월 15일 ~ 전분기 마지막 월 14일**
- 일별 환자 수(24시 기준) = 0시 재원 + 당일 입원 − 당일 퇴원 (6시간 미만 점유 제외,
  전과·전실은 24시 점유 병동 귀속, 외박 포함, 간호간병통합·호스피스 병동 제외)
- 평균: 재원일수 합 ÷ 기간 일수, 소수 셋째자리 반올림 / **비율: 셋째자리 이하 절사**
- 단시간 환산 0.4~0.9 (16h 미만 제외), 연속 부재 16일+ 제외 — 입력측 책임

종별 구간 — **전 종별 확정** (건강보험요양급여비용 2024년 1월판 산정지침 마목 원문,
references/hira-edi-spec/suga-2024-ma-nursing-grade.txt). 의원급은 병상수 기준(별도).
가감률: 최고등급 가산(상급종합 S +15%, 종합 S/A +12% 연쇄) · 하위 등급은 직전의 10% 감산.
재원환자수 자동 집계는 HIS 수신(M0a) 후.
"""
import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

# 종별 등급 구간 (환자수 대 간호사수, 미만 경계) — 산정지침 마목 ⑴ 원문 확정
GRADE_BANDS_BY_TYPE: dict[str, list[tuple[float, str]] | None] = {
    # ㈎ 상급종합병원
    "TERTIARY": [(1.5, "S"), (2.0, "1"), (2.5, "2"), (math.inf, "3")],
    # ㈏ 종합병원
    "GENERAL": [(1.5, "S"), (2.0, "A"), (2.5, "1"), (3.0, "2"),
                (4.0, "3"), (6.0, "4"), (math.inf, "5")],
    # ㈐ 병원·정신병원·치과병원·한방병원
    "HOSPITAL": [(2.0, "A"), (2.5, "1"), (3.0, "2"), (3.5, "3"),
                 (4.0, "4"), (6.0, "5"), (math.inf, "6")],
    # ㈑ 의원·치과의원·한의원·보건의료원 — ⚠️ 병상수 대 간호사수 기준 (환자수 아님)
    "CLINIC_BED_BASIS": [(2.5, "1"), (3.0, "2"), (3.5, "3"),
                         (4.0, "4"), (4.5, "5"), (math.inf, "6")],
}

# 단시간 근무 환산표 (주 근로시간 → 인 환산) — 고시 다.3)나)
PART_TIME_FTE: list[tuple[int, int, float]] = [
    (16, 20, 0.4), (20, 24, 0.5), (24, 28, 0.6),
    (28, 32, 0.7), (32, 36, 0.8), (36, 40, 0.9),
]


@dataclass
class GradeResult:
    avg_patients: float     # 셋째자리 반올림
    avg_nurses: float       # 셋째자리 반올림
    ratio: float            # 환자수 대 간호사수 — 셋째자리 이하 절사
    grade: str | None       # None = 종별 구간 미확정 (비율만 신뢰)
    period_start: date
    period_end: date


def quarter_window(today: date) -> tuple[date, date]:
    """전전분기 마지막 월 15일 ~ 전분기 마지막 월 14일 (고시 가.3)."""
    quarter_start_month = ((today.month - 1) // 3) * 3 + 1
    prev_q_last = date(today.year, quarter_start_month, 1) - timedelta(days=1)  # 전분기 말일
    prev_prev_last_month = prev_q_last.month - 3
    year = prev_q_last.year
    if prev_prev_last_month <= 0:
        prev_prev_last_month += 12
        year -= 1
    return date(year, prev_prev_last_month, 15), prev_q_last.replace(day=14)


def round_avg(value: float) -> float:
    """소수점 셋째자리에서 반올림 → 2자리 (Decimal — float 오차 방지)."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def truncate_ratio(value: float) -> float:
    """소수점 셋째자리 이하 절사 → 2자리 (고시 라.4 — 절사이지 반올림 아님)."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def part_time_fte(weekly_hours: float) -> float:
    """주 근로시간 → 환산 인원. 40h=1.0, 16h 미만=0 (산정 제외)."""
    if weekly_hours >= 40:
        return 1.0
    for lo, hi, fte in PART_TIME_FTE:
        if lo <= weekly_hours < hi:
            return fte
    return 0.0


def compute_grade(*, patient_days: float, nurse_days: float,
                  period_start: date, period_end: date,
                  hospital_type: str = "HOSPITAL") -> GradeResult:
    """재원일수 합·간호사 재직일수 합 → 비율·등급 (구간 미확정 종별은 grade=None)."""
    days = (period_end - period_start).days + 1
    if days <= 0:
        raise ValueError("기간이 올바르지 않습니다")
    bands = GRADE_BANDS_BY_TYPE.get(hospital_type)
    avg_p = round_avg(patient_days / days)
    avg_n = round_avg(nurse_days / days)
    if avg_n <= 0:
        ratio = -1.0
        grade = bands[-1][1] if bands else None  # 간호사 0 = 최저 등급
    else:
        # 나눗셈도 Decimal — float 24.9/10=2.489999… 절사 오류 방지
        ratio = float((Decimal(str(avg_p)) / Decimal(str(avg_n)))
                      .quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        grade = (next(g for limit, g in bands if ratio < limit) if bands else None)
    return GradeResult(avg_patients=avg_p, avg_nurses=avg_n, ratio=ratio, grade=grade,
                       period_start=period_start, period_end=period_end)
