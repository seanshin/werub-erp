"""정기업무(주/월/분기) 주기 엔진 + 핸들러 레지스트리 — 순수 커널(도메인 import 없음).

레지스트리 딕셔너리는 여기 정의하고, 각 도메인/앱 핸들러가 ``@register_handler``로 자기 자신을
등록한다(common은 도메인을 import하지 않음 — import-linter '순수 커널' 계약 준수). 실제 실행 러너는
erp.app.recurring_service(도메인 import 가능 계층)에 둔다.

주기 계산은 KST(Asia/Seoul) 기준 순수 함수. 기획서 docs/01-plan/erp-recurring-operations-plan-2026-07-25.md.
"""
from __future__ import annotations

import calendar
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta

from erp.common.kst import as_naive_wallclock, kst_now

CADENCES = ("DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY")
EXEC_MODES = ("AUTO", "AI_ASSISTED", "MANUAL")

# 핸들러 레지스트리 — code -> async fn(session, period_key: str, ctx: dict) -> dict(result)
HandlerFn = Callable[..., Awaitable[dict]]
_HANDLERS: dict[str, HandlerFn] = {}


def register_handler(code: str) -> Callable[[HandlerFn], HandlerFn]:
    """정기업무 핸들러 등록 데코레이터 — RecurringTask.handler_ref가 이 code를 참조."""
    def deco(fn: HandlerFn) -> HandlerFn:
        if code in _HANDLERS and _HANDLERS[code] is not fn:
            raise ValueError(f"정기업무 핸들러 중복 등록: {code}")
        _HANDLERS[code] = fn
        return fn
    return deco


def get_handler(code: str | None) -> HandlerFn | None:
    return _HANDLERS.get(code) if code else None


def quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


def period_key(cadence: str, d: date) -> str:
    """해당 날짜가 속한 기간의 정규 키 — 멱등 앵커(기간당 1회)."""
    if cadence == "DAILY":
        return d.isoformat()
    if cadence == "WEEKLY":
        iso = d.isocalendar()               # ISO 주(연 경계 W01/W52·W53 정확)
        return f"{iso.year:04d}-W{iso.week:02d}"
    if cadence == "MONTHLY":
        return f"{d.year:04d}-{d.month:02d}"
    if cadence == "QUARTERLY":
        return f"{d.year:04d}-Q{quarter_of(d.month)}"
    if cadence == "YEARLY":
        return f"{d.year:04d}"
    raise ValueError(f"알 수 없는 주기: {cadence}")


def period_bounds(cadence: str, d: date) -> tuple[date, date]:
    """해당 날짜가 속한 기간의 (시작일, 종료일)."""
    if cadence == "DAILY":
        return d, d
    if cadence == "WEEKLY":
        start = d - timedelta(days=d.weekday())      # 월요일 시작
        return start, start + timedelta(days=6)
    if cadence == "MONTHLY":
        last = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, 1), date(d.year, d.month, last)
    if cadence == "QUARTERLY":
        q = quarter_of(d.month)
        sm, em = (q - 1) * 3 + 1, q * 3
        last = calendar.monthrange(d.year, em)[1]
        return date(d.year, sm, 1), date(d.year, em, last)
    if cadence == "YEARLY":
        return date(d.year, 1, 1), date(d.year, 12, 31)
    raise ValueError(f"알 수 없는 주기: {cadence}")


def scheduled_for(cadence: str, d: date, anchor: dict | None) -> datetime:
    """해당 기간의 예정 실행 시각(KST 벽시계 naive).

    anchor={basis: PERIOD_START|PERIOD_END, offset_days, hour, minute}. 예) 부가세 분기확정 =
    QUARTERLY·basis=PERIOD_END·offset_days=25 → 분기종료 익월 25일. 기본=기간 시작 00:00.
    """
    anchor = anchor or {}
    start, end = period_bounds(cadence, d)
    base = end if anchor.get("basis") == "PERIOD_END" else start
    base = base + timedelta(days=int(anchor.get("offset_days", 0)))
    return datetime(base.year, base.month, base.day,
                    int(anchor.get("hour", 0)), int(anchor.get("minute", 0)))


def shift_back(cadence: str, d: date) -> date:
    """직전 기간의 대표 날짜 — 해당 기간 시작 하루 전(= 이전 기간 종료일). catch-up 소급용."""
    return period_bounds(cadence, d)[0] - timedelta(days=1)


DUE_LOOKBACK = 4        # 소급 탐색 상한(기간 수) — 분기 앵커도 1기간이면 충분, 여유 3


def due_period(cadence: str, anchor: dict | None, now: datetime,
               lookback: int = DUE_LOOKBACK) -> date | None:
    """`now` 시점에 **실행 대상인 기간**의 대표 날짜 — 예정 시각이 지난 가장 최근 기간.

    "오늘이 속한 기간"과 다를 수 있다. PERIOD_END 계열 앵커(예: 부가세 = 분기말+25일,
    IT 비용점검 = 월말+3일)는 현재 기간의 예정 시각이 **항상 미래**라, 오늘 기준으로 기간을 잡으면
    아직 끝나지도 않은 기간을 대상으로 삼고(미완결 데이터 점검·오탐 알림), 그 기간의 run 행을
    `UNIQUE(task_code, period_key)`에 선점해 **정규 실행을 영구 차단**한다(적대검토 A-H1).

    반환 None = 아직 어떤 기간도 예정 시각에 도달하지 않음(신규 업무 등).
    """
    d = now.date()
    for _ in range(lookback + 1):
        if scheduled_for(cadence, d, anchor) <= now:
            return d
        d = shift_back(cadence, d)
    return None


def now_naive() -> datetime:
    """현재 KST 벽시계 naive — scheduled_for(naive)와 동일 기준 비교용."""
    return as_naive_wallclock(kst_now())
