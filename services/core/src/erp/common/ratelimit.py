"""인프로세스 슬라이딩윈도우 rate limiter.

온프레미스 단일 서버 전제 — 워커별 독립 상태의 소프트 가드(분산 정합 불요).
외부 LLM 등 비싼 엔드포인트의 사용자별 호출 폭주·증폭 방어용. 분산/영속이 필요하면
redis 백엔드로 교체한다(인터페이스 동일 유지).
"""
import time
from collections import defaultdict, deque

from erp.errors import RateLimitError


class SlidingWindowLimiter:
    """key별 최근 window_s 내 호출 수를 추적, max_calls 초과 시 RateLimitError(429)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, *, max_calls: int, window_s: float) -> None:
        now = time.monotonic()
        dq = self._hits[key]
        cutoff = now - window_s
        while dq and dq[0] <= cutoff:     # 윈도우 밖 기록 제거
            dq.popleft()
        if len(dq) >= max_calls:
            retry_after = int(dq[0] + window_s - now) + 1
            raise RateLimitError(retry_after=max(retry_after, 1))
        dq.append(now)
        if not dq:                        # 방어적 — 빈 deque 누수 정리
            self._hits.pop(key, None)
