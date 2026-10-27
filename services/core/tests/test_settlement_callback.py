"""검진권 정산 지급 회신 송신 (ERP→HIS settlement.paid, DB-4) — 게이트·성공·재시도/DLQ."""
import httpx
from sqlalchemy import select

from erp.config import get_settings
from erp.finance.models import DealerSettlementCallback
from erp.integrations.settlement_callback import send_pending_callbacks


def _queued(event_id="erp:settle-paid:S1", amount=1000000):
    return DealerSettlementCallback(event_id=event_id, settlement_id="S1", distributor_id="D1",
                                    amount_paid_krw=amount, erp_voucher_ref="voucher:settlement:S1",
                                    status="QUEUED")


async def _with(session, fn, *, enabled=True, key="ik", host="https://his.example.org"):
    s = get_settings()
    saved = (s.dealer_settlement_callback_enabled, s.his_integration_key, s.his_eapproval_base)
    s.dealer_settlement_callback_enabled, s.his_integration_key, s.his_eapproval_base = enabled, key, host
    try:
        return await fn(s)
    finally:
        (s.dealer_settlement_callback_enabled, s.his_integration_key, s.his_eapproval_base) = saved


async def test_disabled_skips(session):
    session.add(_queued())
    await session.flush()
    out = await _with(session, lambda _s: send_pending_callbacks(session), enabled=False)
    assert out == {"skipped": "disabled"}


async def test_send_success_marks_sent(session):
    session.add(_queued())
    await session.flush()
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["key"] = req.headers.get("X-Integration-Key")
        seen["idem"] = req.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"received": True})

    out = await _with(session, lambda _s: send_pending_callbacks(
        session, transport=httpx.MockTransport(handler)))
    assert out == {"sent": 1, "failed": 0}
    assert seen["key"] == "ik" and seen["idem"] == "erp:settle-paid:S1"
    cb = await session.scalar(select(DealerSettlementCallback))
    assert cb.status == "SENT" and cb.response_code == 200


async def test_signed_when_secret_set(session):
    """S-2 — 시크릿 설정 시 X-Signature(HMAC rawBody)+X-Timestamp 동봉, 미설정 시 미서명(permissive)."""
    import hashlib
    import hmac as _hmac
    session.add(_queued())
    await session.flush()
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["sig"] = req.headers.get("X-Signature")
        seen["ts"] = req.headers.get("X-Timestamp")
        seen["body"] = req.content
        return httpx.Response(200, json={"received": True})

    s = get_settings()
    old = s.his_finance_webhook_secret
    s.his_finance_webhook_secret = "fsec"
    try:
        await _with(session, lambda _s: send_pending_callbacks(
            session, transport=httpx.MockTransport(handler)))
    finally:
        s.his_finance_webhook_secret = old
    # 서명 입력 = ts + "." + rawBody (타임스탬프 바인딩)
    signed = seen["ts"].encode("utf-8") + b"." + seen["body"]
    expected = "sha256=" + _hmac.new(b"fsec", signed, hashlib.sha256).hexdigest()
    assert seen["sig"] == expected and seen["ts"]


async def test_unsigned_when_secret_unset(session):
    """시크릿 미설정 시 X-Signature 미동봉(permissive — HIS enforce 전 무영향)."""
    session.add(_queued())
    await session.flush()
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["sig"] = req.headers.get("X-Signature")
        return httpx.Response(200, json={"received": True})

    await _with(session, lambda _s: send_pending_callbacks(
        session, transport=httpx.MockTransport(handler)))
    assert seen["sig"] is None


async def test_send_failure_retries_then_dlq(session):
    cb = _queued()
    cb.retry_count = 4                                   # 다음 실패 = 5회째 → DLQ
    session.add(cb)
    await session.flush()

    def boom(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "x"})

    out = await _with(session, lambda _s: send_pending_callbacks(
        session, transport=httpx.MockTransport(boom)))
    assert out == {"sent": 0, "failed": 1}
    refreshed = await session.scalar(select(DealerSettlementCallback))
    assert refreshed.status == "DLQ" and refreshed.retry_count == 5


async def test_ssrf_host_blocked(session):
    session.add(_queued())
    await session.flush()
    out = await _with(session, lambda _s: send_pending_callbacks(session),
                      host="https://evil.example.com")
    assert out == {"skipped": "host_not_allowed"}
