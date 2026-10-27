"""HIS→ERP 전자결재 결과 콜백 — HIS 정본 본문 그대로.

HIS 본문: {approval_id, ref{source_module,source_ref}, result, approved_by, decided_at(ISO), event_id}
**`ts` 없음** · `X-Signature: sha256=<HMAC(본문)>`. 종전 ERP는 `ts` 누락으로 이 콜백 전부를 403
거부했다(HIS 운영 실측: APPROVED·미전달 8건). 아웃박스 재전달은 같은 본문을 나중에 보낸다.
"""
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from erp.config import get_settings
from erp.hr.models import LeaveRequest
from erp.integrations.eapproval import callback_replay_reason
from tests.test_eapproval import _leave

SECRET = "his-eapproval-callback-secret-32bytes!!"


@pytest.fixture
def cb_secret(monkeypatch):
    from erp.common import redis_client
    monkeypatch.setattr(get_settings(), "eapproval_callback_secret", SECRET, raising=False)
    # 공유 redis 클라이언트가 이전 테스트의 이벤트 루프에 묶여 있으면 nonce 해제가 조용히 실패해
    # 실행 순서에 따라 결과가 갈린다(3차 검토 I5) — 이 테스트 루프의 새 클라이언트로.
    monkeypatch.setattr(redis_client, "_redis", None)


def _his_body(leave_id: int, *, decided_at: datetime, result="APPROVED") -> bytes:
    return json.dumps({
        "approval_id": "WS-1", "ref": {"source_module": "hr.leave_requests",
                                       "source_ref": str(leave_id)},
        "result": result, "approved_by": "부서장", "decided_at": decided_at.isoformat(),
        "event_id": f"EVT-{uuid.uuid4().hex}"}).encode()


def _sig(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


async def test_his_contract_callback_applies(session, client, cb_secret):
    _, lv = await _leave(session)
    raw = _his_body(lv.id, decided_at=datetime.now(timezone.utc))
    r = await client.post("/api/v1/integrations/eapproval/callback", content=raw,
                          headers={"X-Signature": _sig(raw), "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    assert (await session.get(LeaveRequest, lv.id)).status == "APPROVED"


async def test_his_outbox_late_redelivery_still_applies(session, client, cb_secret):
    """즉시 3회 실패 → 아웃박스가 몇 시간 뒤 같은 본문을 재전달 — 받아야 결과가 닿는다."""
    _, lv = await _leave(session)
    raw = _his_body(lv.id, decided_at=datetime.now(timezone.utc) - timedelta(hours=6))
    r = await client.post("/api/v1/integrations/eapproval/callback", content=raw,
                          headers={"X-Signature": _sig(raw), "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    assert (await session.get(LeaveRequest, lv.id)).status == "APPROVED"


async def test_unsigned_or_forged_still_rejected(session, client, cb_secret):
    _, lv = await _leave(session)
    raw = _his_body(lv.id, decided_at=datetime.now(timezone.utc))
    for sig in (None, "sha256=" + "0" * 64):
        h = {"Content-Type": "application/json", **({"X-Signature": sig} if sig else {})}
        r = await client.post("/api/v1/integrations/eapproval/callback", content=raw, headers=h)
        assert r.status_code == 403
    assert (await session.get(LeaveRequest, lv.id)).status == "REQUESTED"


def test_replay_reason_rules():
    now = time.time()
    iso = datetime.fromtimestamp(now - 7200, tz=timezone.utc).isoformat()
    assert callback_replay_reason({"decided_at": iso}, now_epoch=now, max_skew=300) is None
    assert callback_replay_reason({"decided_at": "not-a-date"}, now_epoch=now, max_skew=300)
    assert callback_replay_reason({}, now_epoch=now, max_skew=300)            # 시각 없음 → 거부
    # ts를 실은 발신자는 종전 계약(신선도 강제) 유지
    assert callback_replay_reason({"ts": now - 600, "decided_at": iso},
                                  now_epoch=now, max_skew=300)


async def test_callback_does_not_relay_back_to_his(session, monkeypatch):
    """HIS로 되돌려 보내지 않는다(대상 행이 HIS에 없고, 보낸 것이 HIS 자신)."""
    import httpx

    from erp.hr.eapproval_service import EApprovalService
    monkeypatch.setattr(get_settings(), "his_webhook_key", "k", raising=False)
    called = []

    class _Boom(httpx.AsyncClient):
        async def post(self, *a, **kw):
            called.append(a)
            raise AssertionError("HIS 릴레이 호출 금지")
    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    _, lv = await _leave(session)
    out = await EApprovalService().handle_callback(session, body={
        "ref": {"source_module": "hr.leave_requests", "source_ref": str(lv.id)},
        "result": "APPROVED"})
    assert out["updated"] is True and called == []


async def test_failed_processing_does_not_burn_nonce(session, client, cb_secret, monkeypatch):
    """G5 — 처리 실패 시 nonce를 해제해 HIS 재시도(같은 event_id)가 처리된다. 중복은 200."""
    from erp.integrations import eapproval_router
    _, lv = await _leave(session)
    raw = _his_body(lv.id, decided_at=datetime.now(timezone.utc))
    h = {"X-Signature": _sig(raw), "Content-Type": "application/json"}
    real = eapproval_router._svc.handle_callback
    calls = {"n": 0}

    async def flaky(session, *, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("일시 DB 오류")
        return await real(session, body=body)
    monkeypatch.setattr(eapproval_router._svc, "handle_callback", flaky)
    with pytest.raises(RuntimeError):
        await client.post("/api/v1/integrations/eapproval/callback", content=raw, headers=h)
    r = await client.post("/api/v1/integrations/eapproval/callback", content=raw, headers=h)
    assert r.status_code == 200 and r.json().get("updated") is True, r.text
    dup = await client.post("/api/v1/integrations/eapproval/callback", content=raw, headers=h)
    assert dup.status_code == 200 and dup.json()["reason"] == "duplicate"


async def test_inflight_duplicate_gets_409_so_his_retries(session, client, cb_secret, monkeypatch):
    """처리 중인 같은 콜백의 재전송은 200이 아니라 409 — 첫 요청이 커밋에 실패해도 HIS가 다시 보낸다."""
    from erp.common import redis_client
    from erp.common.redis_client import get_redis
    monkeypatch.setattr(redis_client, "_redis", None)     # 이 테스트 루프에 묶인 새 클라이언트
    _, lv = await _leave(session)
    raw = _his_body(lv.id, decided_at=datetime.now(timezone.utc))
    eid = json.loads(raw)["event_id"]
    await get_redis().set(f"eapproval:cb:{eid}", "processing", ex=60)
    r = await client.post("/api/v1/integrations/eapproval/callback", content=raw,
                          headers={"X-Signature": _sig(raw), "Content-Type": "application/json"})
    assert r.status_code == 409
    assert (await session.get(LeaveRequest, lv.id)).status == "REQUESTED"
