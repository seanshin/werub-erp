"""LIS 검사 청구 인바운드 게이트웨이 (E-1: API HMAC 웹훅).

POST /integration/lis/billing — HMAC-SHA256(<ts>.<body>) 검증 → integration.his_inbound_events
멱등 적재(source_ref=lis:{lisBillingId}:{revision}) → 202. consumer(lis.billing)가 캡처·산정.
GET  /integration/lis/billing/{lisBillingId} — 폴링 상태(ACCEPTED/REJECTED/PENDING). 서명 인증.
GET  /integration/lis/billing — 캡처 목록(운영 가시성·chart_no 마스킹). 내부 admin.staging read.
POST /integration/lis/billing/{lisBillingId}/cancel — 파일럿·오전송 관리 취소. admin.staging write.

시크릿 = config.lis_billing_secret(폴백 inbound_webhook_secret). LIS ERP_BILLING_SECRET와 대칭.
"""
import hashlib
import hmac
import logging
import time

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.crypto import mask_chart_no
from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.config import get_settings
from erp.db import get_session
from erp.errors import ForbiddenError, ValidationError
from erp.integrations.his_webhook_router import _signal_inbound, inbound_signature_reason
from erp.integrations.lis_billing_service import LisBillingService
from erp.integrations.models import HisInboundEvent, LisBillingClaim

logger = logging.getLogger("erp.integrations.lis_webhook")
router = APIRouter(prefix="/integration/lis", tags=["integrations"])


def _secret() -> str | None:
    s = get_settings()
    return s.lis_billing_secret or s.inbound_webhook_secret


@router.post("/billing", status_code=202)
async def lis_billing_webhook(request: Request, background: BackgroundTasks,
                              x_integration_signature: str | None = Header(default=None),
                              x_integration_ts: str | None = Header(default=None),
                              session: AsyncSession = Depends(get_session)):
    """LIS→ERP 검사 청구 수신 — HMAC 검증 후 인박스 멱등 적재. consumer가 캡처·산정."""
    raw = await request.body()
    reason = inbound_signature_reason(raw, x_integration_signature, x_integration_ts, _secret(),
                                      now_epoch=time.time(),
                                      max_skew=get_settings().inbound_webhook_max_skew)
    if reason:
        raise ForbiddenError(f"LIS 청구 서명 검증 실패: {reason}")
    import json
    try:
        body = json.loads(raw or b"{}")
    except ValueError as exc:
        raise ValidationError("본문 JSON 파싱 실패") from exc
    if not isinstance(body, dict):
        raise ValidationError("본문은 JSON 객체여야 합니다")
    # ERP 예약 키(_erp*)는 관리 화면만 쓴다 — 송신측이 실어 보낸 것은 버린다(HIS 웹훅과 동일·2026-10-10)
    body = {k: v for k, v in body.items() if not str(k).startswith("_erp")}
    lis_id = body.get("lisBillingId") or body.get("lis_billing_id")
    if not lis_id:
        raise ValidationError("lisBillingId 필수")
    revision = int(body.get("revision") or 1)
    ref = f"lis:{lis_id}:{revision}"
    existing = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == ref))
    if existing is not None:
        # 재전송 회복 — 직전 처리가 FAILED/DEAD면 재큐(payload 갱신·재시도 리셋)해 LIS 재전송
        # 1회로 ACCEPTED 완결. APPLIED/NEW/FAILED-대기중은 중복(재처리 불필요).
        if existing.status in ("FAILED", "DEAD"):
            existing.payload = body
            existing.status, existing.retry_count = "NEW", 0
            existing.next_retry_at, existing.error = None, None
            background.add_task(_signal_inbound)
            return {"received": True, "id": existing.id, "requeued": True}
        return {"received": True, "id": existing.id, "duplicate": True}
    ev = HisInboundEvent(domain="lis", event_type="lis.billing", source_ref=ref,
                         payload=body, status="NEW")
    try:
        async with session.begin_nested():
            session.add(ev)
            await session.flush()
    except IntegrityError:
        return {"received": True, "duplicate": True}
    background.add_task(_signal_inbound)
    return {"received": True, "id": ev.id, "lisBillingId": lis_id, "revision": revision}


@router.get("/billing")
async def list_lis_billing_claims(status: str | None = None, limit: int = 100,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """캡처된 LIS 청구 목록(운영 가시성) — 파일럿 정리·미매핑 보류 확인. ADMIN/DIRECTOR read."""
    require_scope(user, "admin.staging", "read")
    q = select(LisBillingClaim).order_by(LisBillingClaim.id.desc()).limit(min(limit, 500))
    if status:
        q = q.where(LisBillingClaim.status == status)
    rows = (await session.execute(q)).scalars().all()
    return {"count": len(rows), "items": [
        {"id": c.id, "lis_billing_id": c.lis_billing_id, "revision": c.revision,
         "chart_no": mask_chart_no(c.chart_no), "performed_on": c.performed_on,
         "status": c.status, "mapped": bool(c.mapped), "item_count": c.item_count,
         "total_amount": float(c.total_amount) if c.total_amount is not None else None,
         "created_at": c.created_at}
        for c in rows]}


@router.post("/billing/{lis_billing_id}/cancel")
async def cancel_lis_billing_claim(lis_billing_id: str,
                                   user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """파일럿·오전송 청구 관리 취소 — status→CANCELLED(행 보존·감사추적). ADMIN write.

    캡처 스테이징은 실 청구·수납·GL 전이 경로가 없어 회계 영향이 없다. LIS가 상위 revision
    정정을 재전송하면 capture()가 상태를 재판정한다(정정이 관리 취소보다 우선).
    """
    require_scope(user, "admin.staging", "write")
    # 2026-10-09 시나리오 S-B: 같은 취소를 수 ms 간격으로 두 번 보내자 감사 행이 2건 남았다 — 잠금 없이
    # 둘 다 비취소 상태를 읽었고, 커밋이 응답 뒤라 두 번째 요청이 첫 취소를 못 봤다. 행을 FOR UPDATE로
    # 잠그고(두 번째는 첫 커밋 뒤 CANCELLED를 읽어 already) 응답 전에 커밋한다.
    c = await session.scalar(select(LisBillingClaim).where(
        LisBillingClaim.lis_billing_id == lis_billing_id).with_for_update())
    if c is None:
        raise ValidationError("존재하지 않는 청구입니다")
    if c.status == "CANCELLED":
        return {"lis_billing_id": lis_billing_id, "status": "CANCELLED", "already": True}
    before = c.status
    c.status = "CANCELLED"
    logger.info("LIS 청구 관리 취소: %s (actor=%s)", lis_billing_id, user.user_id)
    # 독스트링의 '감사추적'을 실제로 — 종전엔 로그만 남았다(2026-10-09 검토 B)
    from erp.audit.service import AuditService
    await AuditService().record_audit(session, actor=user.user_id, action="LIS_BILLING_CANCEL",
                                      entity="integration.lis_billing_claims",
                                      target_id=lis_billing_id, before={"status": before},
                                      after={"status": "CANCELLED"})
    await session.commit()   # 응답 전 확정 — 이후 DB 접근 없음
    return {"lis_billing_id": lis_billing_id, "status": "CANCELLED"}


@router.get("/billing/{lis_billing_id}")
async def lis_billing_status(lis_billing_id: str,
                             x_integration_signature: str | None = Header(default=None),
                             x_integration_ts: str | None = Header(default=None),
                             session: AsyncSession = Depends(get_session)):
    """폴링 상태 — ACCEPTED(캡처)/REJECTED(DEAD)/PENDING(대기). 서명 인증(signed=<ts>.<id>)."""
    secret = _secret()
    if not secret:
        raise ForbiddenError("LIS 연동 시크릿 미설정")
    if not x_integration_signature or not x_integration_ts:
        raise ForbiddenError("서명/타임스탬프 누락")
    try:
        if abs(time.time() - float(x_integration_ts)) > get_settings().inbound_webhook_max_skew:
            raise ForbiddenError("신선도 창 초과")
    except (TypeError, ValueError) as exc:
        raise ForbiddenError("타임스탬프 형식 오류") from exc
    signed = f"{x_integration_ts}.{lis_billing_id}".encode()
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    got = x_integration_signature[7:] if x_integration_signature.startswith("sha256=") \
        else x_integration_signature
    if not hmac.compare_digest(expected, got.strip()):
        raise ForbiddenError("서명 불일치")
    # 캡처된 청구가 있으면 그 상태, 없으면 인박스 상태로 PENDING/REJECTED 판정
    captured = await LisBillingService().status(session, lis_billing_id=lis_billing_id)
    if captured is not None:
        return captured
    ev = await session.scalar(select(HisInboundEvent).where(
        # autoescape — id의 `_`·`%`가 와일드카드로 남의 이벤트를 집지 않게(시나리오 5회차)
        HisInboundEvent.source_ref.startswith(f"lis:{lis_billing_id}:", autoescape=True))
        .order_by(HisInboundEvent.id.desc()))
    if ev is None:
        return {"lis_billing_id": lis_billing_id, "status": "NOT_FOUND"}
    # RESOLVED(관리자가 사유와 함께 종결)도 종결 상태 — 종전 매핑은 PENDING이라 LIS가 영원히 대기했다(4회차 검토).
    # 다시 청구하려면 LIS가 revision을 올려 재전송한다(같은 revision은 중복 처리).
    if ev.status == "RESOLVED":
        return {"lis_billing_id": lis_billing_id, "status": "REJECTED",
                "error": f"ERP 관리자 종결: {ev.resolve_reason or ''} — 재청구는 revision을 올려 재전송"[:300]}
    status = "REJECTED" if ev.status == "DEAD" else "PENDING"
    return {"lis_billing_id": lis_billing_id, "status": status,
            "error": ev.error if ev.status in ("FAILED", "DEAD") else None}
