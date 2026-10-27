"""요청 경로용 Redis 클라이언트 (lazy) — SSO jti 재생 방지 등.

워커는 자체 redis 연결을 쓰고, API 요청 경로(SSO 소비)는 이 lazy 싱글톤 사용.
연결은 첫 사용 시 — import 시점 부작용 없음.
"""
from erp.config import get_settings

_redis = None

# 완전 실시간 인박스 소비 신호 채널 — 웹훅(발행)·worker 리스너(구독) 공유.
INBOUND_SIGNAL_CHANNEL = "erp:inbound:new"


def get_redis():
    """공유 Redis 클라이언트(redis.asyncio). 연결 실패는 호출부가 graceful 처리."""
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(get_settings().redis_url)
    return _redis
