"""인프로세스 슬라이딩윈도우 rate limiter — 허용·차단·윈도우 만료·키 분리."""
import pytest

from erp.common.ratelimit import SlidingWindowLimiter
from erp.errors import RateLimitError


def test_allows_up_to_max():
    lim = SlidingWindowLimiter()
    for _ in range(3):
        lim.check("k", max_calls=3, window_s=60)   # 예외 없이 통과


def test_blocks_over_max():
    lim = SlidingWindowLimiter()
    lim.check("k", max_calls=2, window_s=60)
    lim.check("k", max_calls=2, window_s=60)
    with pytest.raises(RateLimitError) as ei:
        lim.check("k", max_calls=2, window_s=60)
    assert ei.value.status_code == 429 and ei.value.details["retry_after"] >= 1


def test_keys_isolated():
    lim = SlidingWindowLimiter()
    lim.check("a", max_calls=1, window_s=60)
    lim.check("b", max_calls=1, window_s=60)        # 다른 키는 독립
    with pytest.raises(RateLimitError):
        lim.check("a", max_calls=1, window_s=60)


def test_window_expiry(monkeypatch):
    import erp.common.ratelimit as rl
    t = {"now": 1000.0}
    monkeypatch.setattr(rl.time, "monotonic", lambda: t["now"])
    lim = SlidingWindowLimiter()
    lim.check("k", max_calls=1, window_s=10)
    t["now"] = 1005.0                               # 윈도우 내 → 차단
    with pytest.raises(RateLimitError):
        lim.check("k", max_calls=1, window_s=10)
    t["now"] = 1011.0                               # 윈도우 경과 → 다시 허용
    lim.check("k", max_calls=1, window_s=10)
