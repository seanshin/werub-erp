"""전자결재 콜백 수신 라우터 — HIS→ERP. X-Signature(HMAC) 검증 후 결재 결과 반영.

공개 엔드포인트(인증 헤더 없음) — HMAC 서명으로만 신뢰. 멱등(event_id 중복 무시는 status
멱등 반영으로 충분). 본문 raw bytes로 서명 검증해야 하므로 Request 직접 사용.
"""
import json
import logging
import time

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.redis_client import get_redis
from erp.config import get_settings
from erp.db import get_session
from erp.errors import ForbiddenError, ValidationError
from erp.hr.eapproval_service import EApprovalService
from erp.integrations.eapproval import callback_age_seconds, callback_replay_reason, verify_signature

logger = logging.getLogger("erp.integrations.eapproval")
router = APIRouter(prefix="/integrations/eapproval", tags=["integrations"])
_svc = EApprovalService()


async def _claim_callback_nonce(payload: dict, *, window: int) -> str | None:
    """event_id 단일소비 — 선점 성공 시 키 반환, 이미 처리된 것이면 DuplicateCallback.

    redis 불가·event_id 없음이면 None(ts/decided_at·상태 멱등이 하드 보장). SSO jti(C-3)와 동형.
    ⚠ 선점은 처리·커밋이 **성공한 뒤에만 유지**한다(호출부가 실패 시 해제) — 종전엔 처리 전에 소비해,
    처리·커밋이 실패하면 HIS 재시도(같은 event_id)가 6분간 전부 403으로 막혀 결재 결과가 유실됐다(G5).
    """
    event_id = payload.get("event_id")
    if not event_id:
        return None
    key = f"eapproval:cb:{event_id}"
    try:
        r = get_redis()
        # processing은 짧게(60초) — 처리 중 프로세스가 죽으면 이 표식이 HIS 재시도 범위(약 2분)를 넘겨
        # 남아 결과가 유실된다(3차 검토 I4). 완료(done)만 신선도 창+60초 유지.
        fresh = await r.set(key, "processing", nx=True, ex=60)
        state = None if fresh else await r.get(key)
    except Exception as exc:  # noqa: BLE001 — redis 장애 시 콜백 차단하지 않음(가용성)
        logger.warning("콜백 nonce 검사 생략(redis 불가)", extra={"ctx": {"err": str(exc)[:120]}})
        return None
    if not fresh:
        state = state.decode() if isinstance(state, bytes) else state
        # 처리 중인 중복은 성공으로 답하지 않는다 — 첫 요청이 커밋에 실패하면 결과가 유실되므로
        # HIS가 재시도하게 409(2차 검토: processing/done 2단계).
        raise _DuplicateCallback(in_flight=(state == "processing"))
    return key


class _DuplicateCallback(Exception):
    def __init__(self, in_flight: bool = False):
        super().__init__("duplicate")
        self.in_flight = in_flight


async def _mark_done(key: str | None, window: int) -> None:
    if key is None:
        return
    try:
        await get_redis().set(key, "done", ex=window + 60)
    except Exception:  # noqa: BLE001
        logger.warning("콜백 nonce 완료 표시 실패 — 재전송이 409(재시도)로 응답될 수 있음")


async def _release_nonce(key: str | None) -> None:
    if key is None:
        return
    try:
        await get_redis().delete(key)
    except Exception:  # noqa: BLE001
        logger.warning("콜백 nonce 해제 실패 — 재시도가 창(약 6분) 동안 중복으로 처리될 수 있음")


@router.post("/callback")
async def eapproval_callback(request: Request,
                             x_signature: str | None = Header(default=None),
                             session: AsyncSession = Depends(get_session)):
    """HIS W.Sign 결재 결과 콜백 — HMAC 검증 → hr.leave_requests 반영."""
    s = get_settings()
    secret = s.eapproval_callback_secret
    if not secret:
        raise ForbiddenError("전자결재 콜백 미설정")
    body = await request.body()
    if not verify_signature(body, x_signature, secret):
        raise ForbiddenError("콜백 서명 검증 실패")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValidationError("콜백 본문 파싱 실패") from exc
    # 재생(replay) 방지 — 서명된 ts 신선도(하드 보장) + event_id 단일소비(Redis, 심화)
    reason = callback_replay_reason(
        payload, now_epoch=time.time(), max_skew=s.eapproval_callback_max_skew)
    if reason:
        raise ForbiddenError(f"콜백 재생 방지: {reason}")
    age = callback_age_seconds(payload, now_epoch=time.time())
    if age is not None and age > s.eapproval_callback_max_skew:
        # 늦은 재전달(HIS 아웃박스 재시도 등)은 받되 흔적을 남긴다 — 사람이 지연을 볼 수 있게.
        logger.info("전자결재 콜백 지연 수신(재전달)", extra={"ctx": {
            "age_seconds": int(age), "event_id": payload.get("event_id")}})
    try:
        key = await _claim_callback_nonce(payload, window=s.eapproval_callback_max_skew)
    except _DuplicateCallback as dup:
        if dup.in_flight:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=409, content={
                "error": {"code": "ERP-EAPPROVAL-INFLIGHT",
                          "message": "같은 콜백을 처리 중입니다 — 잠시 후 재시도하세요"}})
        # 이미 처리 완료된 콜백 — 200으로 답한다. 403이면 응답이 유실돼 HIS가 재시도한 경우 HIS 원장이
        # 실패로 남는다. 처리는 REQUESTED에서만 전이하므로 다시 받아도 상태는 같다.
        return {"ignored": True, "reason": "duplicate", "event_id": payload.get("event_id")}
    try:
        out = await _svc.handle_callback(session, body=payload)
        await session.commit()          # 커밋까지 성공해야 nonce를 유지한다
    except Exception:
        await _release_nonce(key)
        raise
    await _mark_done(key, s.eapproval_callback_max_skew)
    return out
