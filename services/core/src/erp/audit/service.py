"""감사·접속기록 서비스 — design §3.5.

접속기록은 응답 경로 밖 비동기(Redis Stream 적재 → worker-batch가 기록).
적재 실패 시 동기 폴백 — 유실 금지 (NFR).
"""
import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.models import AccessLog, AuditLog


def _jsonable(d: dict | None) -> dict | None:
    """date·Decimal 등 비직렬 값을 문자열화해 JSON 컬럼 저장을 보장(감사 payload는 임의 도메인 dict)."""
    if d is None:
        return None
    return json.loads(json.dumps(d, default=str, ensure_ascii=False))

logger = logging.getLogger("erp.audit")

ACCESS_STREAM = "erp:access_log"


class AuditService:
    def __init__(self, redis=None):
        self._redis = redis

    async def record_access(self, session: AsyncSession, *, actor: int, action: str,
                            entity: str, target_id: str | None = None,
                            route: str | None = None) -> None:
        """접속기록 — Redis Stream 우선(비동기), 실패 시 동기 INSERT 폴백."""
        payload = {"actor": actor, "action": action, "entity": entity,
                   "target_id": target_id or "", "route": route or ""}
        if self._redis is not None:
            try:
                await self._redis.xadd(ACCESS_STREAM, {"data": json.dumps(payload)})
                return
            except Exception:  # noqa: BLE001 — 폴백 경로
                # target_id(차트번호 가능)는 로그 제외(INV-TEL-01) — 동기 INSERT에 온전히 남음
                logger.warning("access_log stream 적재 실패 — 동기 폴백",
                               extra={"ctx": {"actor": actor, "entity": entity}})
        session.add(AccessLog(actor=actor, action=action, entity=entity,
                              target_id=target_id, route=route))

    async def record_audit(self, session: AsyncSession, *, actor: int | None, action: str,
                           entity: str, target_id: str | None = None,
                           before: dict | None = None, after: dict | None = None) -> None:
        """변경 감사 — 트랜잭션 내 동기 기록 (변경과 원자적으로 묶임).

        before/after는 임의 도메인 dict이라 date·Decimal 등 JSON 비직렬 값이 섞일 수 있어
        JSON-safe로 강제(default=str)한다 — Postgres JSON 컬럼 직렬화 실패 방지.
        """
        session.add(AuditLog(actor=actor, action=action, entity=entity, target_id=target_id,
                             before=_jsonable(before), after=_jsonable(after)))


async def flush_access_stream(redis, session: AsyncSession, *, batch: int = 500) -> list:
    """worker-batch 잡 — Stream 소비 → access_log INSERT. 소비한 msg_id 목록 반환.

    ⚠ 스트림 삭제(xdel)는 반드시 **DB 커밋 성공 후** 호출측이 수행(delete-after-commit 불변식).
    커밋 전 삭제하면 커밋 실패 시 법정 2년 접속기록이 Redis·DB 양쪽에서 사라져 영구 유실.
    """
    entries = await redis.xrange(ACCESS_STREAM, count=batch)
    ids: list = []
    for msg_id, fields in entries:
        data = json.loads(fields[b"data"] if b"data" in fields else fields["data"])
        session.add(AccessLog(actor=int(data["actor"]), action=data["action"],
                              entity=data["entity"], target_id=data["target_id"] or None,
                              route=data["route"] or None))
        ids.append(msg_id)
    return ids
