"""worker-integration 엔트리포인트 — AI 발신 루프 + SENT 폴링 (design §2.1).

견고성 (분석 H4/H5): 백오프 sleep은 트랜잭션 밖, 루프는 어떤 예외에도
sleep 후 재시도 (busy-loop 방지), entry 비바인딩 참조 제거.
"""
import asyncio
import logging
import os

from erp.common.kst import assert_process_tz_kst
from erp.db import session_factory
from erp.integrations.ai_publisher import HttpAIIndexPublisher
from erp.logging import setup_logging
from erp.workers.ai_dispatch import POLL_INTERVAL_IMMEDIATE, claim_next, dispatch_one, poll_sent

logger = logging.getLogger("erp.workers.integration")

IDLE_SLEEP = 5
ERROR_BACKOFF = 10
RETRY_BACKOFF = 5


def _publisher() -> HttpAIIndexPublisher | None:
    base = os.environ.get("WEVE_AI_API_BASE")
    key = os.environ.get("WEVE_AI_API_KEY")
    if not base or not key:
        return None  # 화이트리스트·키 미설정 — 발신 비활성 (큐 적재만)
    return HttpAIIndexPublisher(base, key)


HEARTBEAT_KEY = "erp:heartbeat:worker-integration"
_TICKS: dict[str, float] = {}      # 루프별 마지막 반복 시각 — 컨테이너는 살아 있는데 루프가 멈춘 상태 감지


def _tick(name: str) -> None:
    import time
    _TICKS[name] = time.time()


async def heartbeat_loop() -> None:
    """하트비트 — 활성 루프가 모두 10분 안에 반복했을 때만 기록(2026-10-09 검토 H: 하트비트·헬스체크 부재로
    소비가 멈춘 행업을 아무도 감지하지 못했다). 발신 미설정(대기 모드)이면 루프가 없으므로 항상 기록."""
    import time

    from erp.common.redis_client import get_redis
    while True:
        try:
            now = time.time()
            if all(now - t < 600 for t in _TICKS.values()):
                await get_redis().set(HEARTBEAT_KEY, int(now), ex=900)
        except Exception as exc:  # noqa: BLE001
            logger.warning("heartbeat 실패", extra={"ctx": {"err": str(exc)[:120]}})
        await asyncio.sleep(60)


async def dispatch_loop() -> None:
    publisher = _publisher()
    if publisher is None:
        logger.warning("AI publisher 미설정 — 발신 루프 대기 모드 (WEVE_AI_API_* 필요)")
        await asyncio.Event().wait()
        return
    while True:
        _tick("dispatch")
        processed = False
        sent_ok = True
        try:
            async with session_factory()() as session:
                async with session.begin():
                    entry = await claim_next(session)
                    if entry is not None:
                        processed = True
                        sent_ok = await dispatch_one(session, publisher, entry)
            # 트랜잭션 커밋 후 대기 (H4 — 커넥션 비점유 백오프)
            if not processed:
                await asyncio.sleep(IDLE_SLEEP)
            elif not sent_ok:
                await asyncio.sleep(RETRY_BACKOFF)
        except Exception as exc:  # noqa: BLE001 — 상주 루프는 죽지 않는다 (H5)
            logger.error("dispatch_loop 오류 — 백오프 후 재시도",
                         extra={"ctx": {"err": str(exc)[:120]}})
            await asyncio.sleep(ERROR_BACKOFF)


async def poll_loop() -> None:
    publisher = _publisher()
    if publisher is None:
        await asyncio.Event().wait()
        return
    while True:
        _tick("poll")
        try:
            async with session_factory()() as session:
                # poll_sent가 read/HTTP/write 트랜잭션을 자체 관리(HTTP는 트랜잭션 밖) — 외부 begin 제거
                await poll_sent(session, publisher)
        except Exception as exc:  # noqa: BLE001 (H5)
            logger.error("poll_loop 오류 — 백오프 후 재시도",
                         extra={"ctx": {"err": str(exc)[:120]}})
        await asyncio.sleep(POLL_INTERVAL_IMMEDIATE)


async def main() -> None:
    setup_logging()
    assert_process_tz_kst()   # naive KST → timestamptz 쓰기 정합의 전제(불일치=기동 중단)
    logger.info("worker-integration 기동")
    await asyncio.gather(dispatch_loop(), poll_loop(), heartbeat_loop())


if __name__ == "__main__":
    asyncio.run(main())
