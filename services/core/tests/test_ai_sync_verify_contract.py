"""AI Server 공시 동기화 검증 — 수신부 정본 계약.

AI Server routes/notice.py: 요청 {"snapshot": [...]}(없으면 400 'snapshot array required'),
응답 {"total","matched","mismatched":[...]}. 2026-10-07 이전 서버는 `items`를 거부한다.
"""
import json

import httpx

from erp.config import get_settings
from erp.integrations import ai_publisher


def _patch(monkeypatch, handler):
    real = httpx.AsyncClient

    class _C(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(ai_publisher.httpx, "AsyncClient", _C)
    monkeypatch.setattr(get_settings(), "ai_allowed_hosts", "ai", raising=False)


async def test_sync_verify_sends_snapshot_and_reads_mismatched(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.update(body)
        if not isinstance(body.get("snapshot"), list):     # 구버전 AI Server 동작
            return httpx.Response(400, json={"error": "snapshot array required"})
        return httpx.Response(200, json={"total": 1, "matched": 0, "mismatched": [
            {"erp_ref_id": "E-1", "reason": "hash_diff"}]})
    _patch(monkeypatch, handler)
    pub = ai_publisher.HttpAIIndexPublisher("http://ai/api/notice", "k")
    out = await pub.sync_verify([{"erp_ref_id": "E-1", "revision_no": 1, "content_hash": "x"}])
    assert "items" not in seen and out == [{"erp_ref_id": "E-1", "reason": "hash_diff"}]


async def test_sync_verify_accepts_legacy_mismatches_key(monkeypatch):
    _patch(monkeypatch, lambda req: httpx.Response(200, json={"mismatches": [{"erp_ref_id": "E"}]}))
    pub = ai_publisher.HttpAIIndexPublisher("http://ai/api/notice", "k")
    assert await pub.sync_verify([]) == [{"erp_ref_id": "E"}]
