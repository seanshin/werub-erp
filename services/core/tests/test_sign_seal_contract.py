"""자금결재 원장 sign 외부 봉인 — sign 실계약 대조.

가짜 sign은 실제 sign 코드(audit-event.controller·dto·audit-filter·pagination)와 같은 규칙을 따른다:
POST는 `actor` 필수(없으면 400) · 항목 유형 필드는 `eventType` · 목록은 seq **오름차순** ·
`eventType` 서버 필터 · `{data, page:{total,limit,offset}}`.
"""
import httpx
import pytest

from erp.config import get_settings
from erp.integrations import sign_client
from tests.test_approval_ledger import _flow


class FakeSign:
    def __init__(self):
        self.entries: list[dict] = []
        self.posts: list[dict] = []
        self.fail_next_post = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        import json
        if req.method == "POST" and req.url.path.endswith("/audit-events"):
            body = json.loads(req.content)
            self.posts.append(body)
            if self.fail_next_post:
                self.fail_next_post = False
                return httpx.Response(503, json={"message": "down"})
            if not body.get("actor"):
                return httpx.Response(400, json={"message": ["actor must be a string"]})
            entry = {"eventType": body["action"], "occurredAt": f"2026-10-0{len(self.entries) + 1}T00:00:00Z",
                     "data": {"actor": body["actor"], **(body.get("data") or {})},
                     "entryHash": f"{len(self.entries):064d}"}
            self.entries.append(entry)
            return httpx.Response(201, json={"data": {"entryHash": entry["entryHash"],
                                                      "anchored": bool(body.get("anchor"))}})
        if req.method == "GET" and req.url.path.endswith("/audit-events"):
            q = req.url.params
            rows = [e for e in self.entries
                    if not q.get("eventType") or e["eventType"] == q["eventType"]]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", len(rows) or 1))
            return httpx.Response(200, json={"data": rows[off:off + lim],
                                             "page": {"total": len(rows), "limit": lim, "offset": off}})
        return httpx.Response(404)


@pytest.fixture
def fake_sign(monkeypatch):
    fs = FakeSign()
    monkeypatch.setattr(get_settings(), "sign_api_key", "test-key", raising=False)
    monkeypatch.setattr(sign_client, "_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(fs.handler), base_url="http://sign/v1"))
    return fs


async def test_record_sends_actor(fake_sign):
    await sign_client.record_audit_event(stream="s", action="LEDGER_ANCHOR", target_type="t",
                                         target_ref="r", data={})
    assert fake_sign.posts[0]["actor"] == sign_client.ERP_AUDIT_ACTOR


async def test_latest_returns_newest_of_type(fake_sign):
    """오름차순 목록에서 **최신** 항목을, 다른 유형은 제외하고 고른다."""
    fake_sign.entries = [
        {"eventType": "LEDGER_ANCHOR", "data": {"seq_no": 1}},
        {"eventType": "OTHER", "data": {"seq_no": 99}},
        {"eventType": "LEDGER_ANCHOR", "data": {"seq_no": 5}},
        {"eventType": "LEDGER_ANCHOR", "data": {"seq_no": 9}},
    ]
    got = await sign_client.latest_audit_event(stream="s", event_type="LEDGER_ANCHOR")
    assert got["data"]["seq_no"] == 9
    assert await sign_client.latest_audit_event(stream="s", event_type="NONE") is None


async def test_verify_job_seals_and_audits_against_real_contract(session, fake_sign):
    """제출이 실제로 성립하고(201), 같은 실행의 대조가 그 봉인을 찾아 원장과 일치를 확인한다."""
    from erp.app.recurring_handlers import approval_ledger_verify
    await _flow(session, doc_id=41)
    out = await approval_ledger_verify(session, "2026-10-08", {})
    assert out["ok"] is True and "error" not in out["sign"], out
    assert out["sign_audit"]["match"] is True                 # 종전: '외부 봉인 이력 없음'
    # 재실행 — 이미 봉인된 앵커는 다시 제출하지 않는다
    out2 = await approval_ledger_verify(session, "2026-10-08", {})
    assert "sign" not in out2 and len(fake_sign.entries) == 1


async def test_failed_seal_is_retried_next_run(session, fake_sign):
    """제출 실패한 앵커는 다음 실행에서 재제출된다(종전: 앵커가 이미 있어 영구 미봉인)."""
    from erp.app.recurring_handlers import approval_ledger_verify
    await _flow(session, doc_id=42)
    fake_sign.fail_next_post = True
    out = await approval_ledger_verify(session, "2026-10-08", {})
    assert "error" in out["sign"] and fake_sign.entries == []
    out2 = await approval_ledger_verify(session, "2026-10-09", {})   # 신규 엔트리 없음
    assert out2["sign"]["catch_up_seq"] >= 1 and len(fake_sign.entries) == 1
    assert out2["sign_audit"]["match"] is True


async def test_tampered_seal_raises_alarm(session, fake_sign):
    """sign의 최신 봉인 head가 원장과 다르면 변조 경보 — 대조가 실제로 동작해야 잡힌다."""
    from sqlalchemy import select

    from erp.app.recurring_handlers import approval_ledger_verify
    from erp.common.models import Notification
    await _flow(session, doc_id=43)
    await approval_ledger_verify(session, "2026-10-08", {})
    fake_sign.entries[-1]["data"]["head_hash"] = "f" * 64
    out = await approval_ledger_verify(session, "2026-10-09", {})
    assert out["sign_audit"]["match"] is False
    n = (await session.scalars(select(Notification).where(
        Notification.type == "APPROVAL_LEDGER_TAMPER"))).all()
    assert n
