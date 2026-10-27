"""검진권 정산 지급 회신 송신 (ERP→HIS settlement.paid, DB-4).

아웃박스(finance.dealer_settlement_callbacks)의 QUEUED/재시도 도래분을 HIS 콜백 EP로 POST.
게이트(dealer_settlement_callback_enabled)·SSRF 화이트리스트·TLS verify·멱등(Idempotency-Key)·
선형 백오프(5회→DLQ). 송신 실패는 격리(다음 회차 재시도) — 부킹 트랜잭션과 분리.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.integrations.egress import allowed_hosts
from erp.config import get_settings
from erp.finance.models import DealerSettlementCallback

logger = logging.getLogger("erp.integrations.settlement_callback")
MAX_RETRY = 5


async def send_pending_callbacks(session: AsyncSession, *, limit: int = 50,
                                 transport: httpx.BaseTransport | None = None) -> dict:
    s = get_settings()
    if not s.dealer_settlement_callback_enabled:
        return {"skipped": "disabled"}
    if not s.his_integration_key:
        return {"skipped": "no_key"}
    base = s.his_eapproval_base.rstrip("/")
    if urlparse(base).hostname not in allowed_hosts("his", loopback=True):   # SSRF 방어
        logger.warning("정산 회신 차단 — 허용되지 않은 대상",
                       extra={"ctx": {"host": urlparse(base).hostname}})
        return {"skipped": "host_not_allowed"}

    now = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(DealerSettlementCallback).where(or_(
            DealerSettlementCallback.status == "QUEUED",
            and_(DealerSettlementCallback.status == "FAILED",
                 or_(DealerSettlementCallback.next_retry_at.is_(None),
                     DealerSettlementCallback.next_retry_at <= now))))
        .order_by(DealerSettlementCallback.id).limit(limit))).scalars().all()
    if not rows:
        return {"sent": 0, "failed": 0}

    sent = failed = 0
    dlq_new: list = []
    url = f"{base}{s.settlement_callback_path}"
    async with httpx.AsyncClient(timeout=10.0, verify=True, transport=transport) as client:
        for cb in rows:
            payload = {
                "event_id": cb.event_id, "settlementId": cb.settlement_id,
                "distributorId": cb.distributor_id, "status": "PAID", "paidAt": now.isoformat(),
                "erpVoucherRef": cb.erp_voucher_ref, "amountPaidKrw": float(cb.amount_paid_krw),
                "withholdingKrw": float(cb.withholding_krw) if cb.withholding_krw is not None else None,
                "fxRateApplied": float(cb.fx_rate) if cb.fx_rate is not None else None,
            }
            # rawBody 고정(서명 바이트 == 송신 바이트) — content= 송신, Content-Type 명시.
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"X-Integration-Key": s.his_integration_key,
                       "Idempotency-Key": cb.event_id,
                       "Content-Type": "application/json"}
            if s.his_finance_webhook_secret:   # S-2 본문 서명(설정 시)
                ts = datetime.now(timezone.utc).isoformat()
                # 서명 입력 = X-Timestamp + "." + rawBody → 타임스탬프 바인딩(ts 위조 시 서명 불일치,
                # 신선도창 밖 재전송 차단). HIS 권장(rawBody-only 레거시도 한시 수용).
                signed = ts.encode("utf-8") + b"." + body
                sig = hmac.new(s.his_finance_webhook_secret.encode(), signed,
                               hashlib.sha256).hexdigest()
                headers["X-Signature"] = f"sha256={sig}"
                headers["X-Timestamp"] = ts
            try:
                r = await client.post(url, content=body, headers=headers)
                r.raise_for_status()
                cb.status, cb.sent_at, cb.response_code, cb.error = "SENT", now, r.status_code, None
                cb.next_retry_at = None   # retry_count는 시도 이력으로 남긴다(몇 번 만에 성공했는지)
                sent += 1
            except Exception as exc:  # noqa: BLE001 — 송신 실패 격리·재시도
                cb.retry_count += 1
                cb.error = str(exc)[:300]
                cb.response_code = getattr(getattr(exc, "response", None), "status_code", None)
                if cb.retry_count >= MAX_RETRY:
                    cb.status, cb.next_retry_at = "DLQ", None
                    dlq_new.append(cb.event_id)
                else:
                    cb.status = "FAILED"
                    cb.next_retry_at = now + timedelta(minutes=2 * cb.retry_count)
                failed += 1
                logger.warning("정산 회신 송신 실패", extra={"ctx": {
                    "event_id": cb.event_id, "retry": cb.retry_count, "err": str(exc)[:120]}})
    if dlq_new:
        # DLQ에 알림이 없어 HIS가 20분 넘게 멈추면 지급 회신이 조용히 쌓였다(2026-10-09 검토 F)
        from erp.common.models import Notification, User
        for uid in (await session.scalars(select(User.id).where(
                User.role == "ADMIN", User.is_active.is_(True)))).all():
            session.add(Notification(
                user_id=uid, type="SETTLEMENT_CALLBACK_DLQ", title=f"검진권 정산 지급 회신 실패 {len(dlq_new)}건",
                body="HIS 정산 회신이 재시도 한도를 넘었습니다 — HIS 상태 확인 후 재전송(재큐) 필요"))
    return {"sent": sent, "failed": failed}
