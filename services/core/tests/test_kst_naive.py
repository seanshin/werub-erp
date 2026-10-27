"""as_naive_wallclock — PG(UTC-라벨 aware)↔sqlite(naive) 벽시계 통일 헬퍼(P2-7).

전제: 저장 시 KST 벽시계 naive로 넣으므로, 되읽힌 aware는 '벽시계 숫자 = KST'에 UTC 라벨만 붙은 것.
따라서 astimezone 환산이 아니라 tzinfo 제거(벽시계 보존)가 정답.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from erp.common.kst import as_naive_wallclock

UTC = ZoneInfo("UTC")


def test_none_passthrough():
    assert as_naive_wallclock(None) is None


def test_naive_passthrough():
    d = datetime(2026, 6, 15, 18, 2)
    assert as_naive_wallclock(d) == d and as_naive_wallclock(d).tzinfo is None


def test_aware_strips_label_preserving_wallclock():
    # PG timestamptz가 되읽는 형태: 벽시계 18:02(KST 값)에 UTC 라벨 → 라벨만 제거해 18:02 보존
    d = datetime(2026, 6, 15, 18, 2, tzinfo=UTC)
    out = as_naive_wallclock(d)
    assert out.tzinfo is None and out.hour == 18 and out.minute == 2
