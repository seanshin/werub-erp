"""이의신청 기한 산정 (FR-40, G5) — 법령 기반 확정 구현.

근거 (references — 2026-06-07 조사):
- 국민건강보험법 §87: 처분이 있음을 **안 날부터 90일**, 처분이 **있은 날부터 180일** 상한
- 민법 §157 초일불산입: 도달일 다음날부터 기산
- 민법 §161: 기간 말일이 **토요일 또는 공휴일**이면 익일로 만료

⚠️ G5 잔여: 음력 명절·대체공휴일은 연도별 테이블(EXTRA_HOLIDAYS) 주입 필요 —
   고정 양력 공휴일·주말은 코드로 처리. 운영 전 해당 연도 공휴일 테이블 등록 확인.
"""
from datetime import date, timedelta

# 고정 양력 공휴일 (월, 일)
FIXED_HOLIDAYS: set[tuple[int, int]] = {
    (1, 1), (3, 1), (5, 5), (6, 6), (8, 15), (10, 3), (10, 9), (12, 25),
}

# 시행 연도가 있는 고정 공휴일 — (월, 일) → (시행 연도, 명칭). '관공서의 공휴일에 관한 규정' 개정(2026-04-28 국무회의 의결·
# 「공휴일에 관한 법률」 개정 후속)으로 노동절(종전 근로자의 날)·제헌절이 2026년부터 관공서 공휴일 — 2026-10-11까지 코드·DB 어디에도
# 없어 그날 근무의 휴일근로 가산·청구 공휴가산·기한이 평일로 계산됐다(prod 근태 0건이라 실금액 영향 0).
FIXED_HOLIDAYS_SINCE: dict[tuple[int, int], tuple[int, str]] = {
    (5, 1): (2026, "노동절"),
    (7, 17): (2026, "제헌절"),
}


def is_fixed_holiday(d: date) -> bool:
    """고정 양력 공휴일(시행 연도 반영) — 급여 휴일근로·청구 공휴가산·이의신청 기한의 단일 판정."""
    if (d.month, d.day) in FIXED_HOLIDAYS:
        return True
    since = FIXED_HOLIDAYS_SINCE.get((d.month, d.day))
    return since is not None and d.year >= since[0]


# 연도별 추가 공휴일 (음력 명절·대체공휴일) — 운영 시 연 1회 갱신 (월력요항 발표분)
# 평일에 해당하는 날만 등록 (주말은 is_holiday가 처리). 누락 시 기한이 앞당겨지는
# 안전 방향이지만 정확성을 위해 매년 12월 차년도분 등록 필수.
EXTRA_HOLIDAYS: dict[int, set[tuple[int, int]]] = {
    2026: {
        (2, 16), (2, 17), (2, 18),  # 설 연휴 (설날 2/17 화)
        (3, 2),                      # 3·1절 대체 (3/1 일)
        (5, 25),                     # 부처님오신날 대체 (5/24 일)
        (8, 17),                     # 광복절 대체 (8/15 토)
        (9, 24), (9, 25),            # 추석 연휴 평일분 (추석 9/25 금)
        (10, 5),                     # 개천절 대체 (10/3 토)
    },
}

APPEAL_DAYS = 90          # 안 날부터 (법 §87)
DISPOSED_CAP_DAYS = 180   # 있은 날부터 (법 §87)


def is_holiday(d: date, extra_dates: set[date] | None = None) -> bool:
    """공휴일 여부 — 주말·고정 양력·DB 공휴일 캘린더(extra_dates)·코드 EXTRA_HOLIDAYS의 **합집합**.

    종전엔 DB 캘린더가 비어 있지 않으면 코드 표를 버렸다 — 선거일 하나만 등록해도 그 해 설·추석이 기한 계산에서
    빠졌다(2026-10-11 검토·급여 `pay_inputs`·공휴가산 `pre_review`는 이미 합집합이었다)."""
    if d.weekday() >= 5:  # 토(5)·일(6) — 민법 §161 토요일 포함
        return True
    if is_fixed_holiday(d):
        return True
    if extra_dates and d in extra_dates:
        return True
    return (d.month, d.day) in EXTRA_HOLIDAYS.get(d.year, set())


def appeal_deadline(arrived_on: date, *, disposed_on: date | None = None,
                    extra_dates: set[date] | None = None) -> date:
    """이의신청 법정 기한.

    초일불산입(§157) → 도달 다음날부터 90일 → 말일이 토/공휴일이면 익일(§161).
    disposed_on(처분일)이 주어지면 180일 상한 적용 (둘 중 이른 날).
    extra_dates: DB 공휴일 캘린더(변동 공휴일) 주입 — 없으면 코드 폴백.
    """
    base = arrived_on + timedelta(days=APPEAL_DAYS)  # 초일불산입 + 90일 = 도달일+90
    while is_holiday(base, extra_dates):
        base += timedelta(days=1)  # §161 — 익일 만료 (연휴면 연쇄)

    if disposed_on is not None:
        cap = disposed_on + timedelta(days=DISPOSED_CAP_DAYS)
        while is_holiday(cap, extra_dates):
            cap += timedelta(days=1)
        return min(base, cap)
    return base
