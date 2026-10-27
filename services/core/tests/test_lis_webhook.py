"""LIS 청구 웹훅 게이트웨이 — HMAC 검증·멱등 staging·재전송 회복(FAILED/DEAD 재큐)."""
import hashlib
import hmac
import json
import time

import pytest
from sqlalchemy import func, select

from fastapi import BackgroundTasks

from erp.config import get_settings
from erp.errors import ForbiddenError
from erp.integrations.lis_webhook_router import lis_billing_webhook
from erp.integrations.models import HisInboundEvent


class _Req:
    def __init__(self, raw: bytes): self._raw = raw
    async def body(self) -> bytes: return self._raw


def _sign(secret: str, ts: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()


async def _post(session, secret, body: dict):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    return await lis_billing_webhook(
        request=_Req(raw), background=BackgroundTasks(),
        x_integration_signature=_sign(secret, ts, raw), x_integration_ts=ts, session=session)


async def _with_secret(fn):
    s = get_settings()
    old = s.lis_billing_secret
    s.lis_billing_secret = "lis_secret_test"
    try:
        return await fn("lis_secret_test")
    finally:
        s.lis_billing_secret = old


async def test_stage_and_idempotent(session):
    async def run(secret):
        body = {"lisBillingId": "LIS-O-1", "revision": 1, "items": [{"testCode": "L1"}]}
        out = await _post(session, secret, body)
        assert out["received"] and out.get("duplicate") is not True
        dup = await _post(session, secret, body)
        assert dup["duplicate"] is True                 # 동일 id+rev 재전송(APPLIED 전) = 중복
        assert (await session.scalar(select(func.count()).select_from(HisInboundEvent))) == 1
    await _with_secret(run)


async def test_bad_signature_rejected(session):
    async def run(_secret):
        raw = json.dumps({"lisBillingId": "X", "items": [{}]}).encode()
        ts = str(int(time.time()))
        with pytest.raises(ForbiddenError, match="서명"):
            await lis_billing_webhook(request=_Req(raw), background=BackgroundTasks(),
                                      x_integration_signature="sha256=deadbeef", x_integration_ts=ts,
                                      session=session)
    await _with_secret(run)


async def test_requeue_on_resend_after_dead(session):
    async def run(secret):
        body = {"lisBillingId": "LIS-O-2", "revision": 1, "items": [{"testCode": "L1"}]}
        out = await _post(session, secret, body)
        ev = await session.get(HisInboundEvent, out["id"])
        ev.status, ev.error = "DEAD", "handler 미구현"     # 초기 실패 시뮬
        await session.flush()
        # 재전송 → 재큐(NEW·에러 리셋)
        again = await _post(session, secret, body)
        assert again.get("requeued") is True
        ev2 = await session.get(HisInboundEvent, out["id"])
        assert ev2.status == "NEW" and ev2.error is None and ev2.retry_count == 0
        assert (await session.scalar(select(func.count()).select_from(HisInboundEvent))) == 1
    await _with_secret(run)
