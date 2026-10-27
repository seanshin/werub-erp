"""e-sign 트랙 A(외부 거래처 계약, ERP↔sign 직접) — webhook 수신 + 개시 오케스트레이션.

webhook: X-Sign-Signature=HMAC(rawBody, ERP_WEBHOOK_SECRET)(소비자별 격리) 검증 → externalDocId
`oc:<id>` → OutsourcingContract 미러. 개시: enroll→requests→portal-tokens(클라이언트는 모킹).
"""
import hashlib
import hmac
import json
from datetime import date

import pytest
from starlette.requests import Request

from erp.config import get_settings
from erp.errors import ForbiddenError, ValidationError
from erp.integrations import sign_webhook_router as swr
from erp.integrations.sign_initiate import initiate_outsourcing_signing
from erp.scm.models import OutsourcingContract

SECRET = "erp_wh_secret_xyz"


def _sign(raw: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _request(raw: bytes, signature: str | None) -> Request:
    headers = [(b"content-type", b"application/json")]
    if signature is not None:
        headers.append((b"x-sign-signature", signature.encode()))
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


async def _oc(session, **kw):
    defaults = dict(vendor_name="청정급식", category="MEAL", title="급식 위탁",
                    start_on=date(2026, 1, 1), end_on=date(2026, 12, 31))
    defaults.update(kw)
    oc = OutsourcingContract(**defaults)
    session.add(oc)
    await session.flush()
    return oc


@pytest.fixture
def _secret(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "sign_webhook_secret", SECRET, raising=False)
    return s


# ── _verify ───────────────────────────────────────────────
def test_verify_good_bad_unset(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "sign_webhook_secret", SECRET, raising=False)
    raw = b'{"a":1}'
    swr._verify(raw, _sign(raw))                       # 통과
    swr._verify(raw, "sha256=" + _sign(raw))           # 접두사 허용
    with pytest.raises(ForbiddenError, match="불일치"):
        swr._verify(raw, "deadbeef")
    with pytest.raises(ForbiddenError, match="불일치"):
        swr._verify(raw, None)
    monkeypatch.setattr(s, "sign_webhook_secret", None, raising=False)
    with pytest.raises(ForbiddenError, match="미설정"):
        swr._verify(raw, _sign(raw))


def test_contract_id_parsing():
    assert swr._contract_id("oc:42") == 42
    assert swr._contract_id("erpcontract:medical:7") is None
    assert swr._contract_id("oc:abc") is None
    assert swr._contract_id(None) is None


# ── webhook 통합 ───────────────────────────────────────────
async def test_webhook_mirrors_outsourcing(session, _secret):
    oc = await _oc(session)
    payload = {"eventType": "request.completed", "eventId": "EV-1",
               "requestId": "REQ-9", "externalDocId": f"oc:{oc.id}", "status": "COMPLETED",
               "signCertSerial": "AA:BB", "auditCertUri": "/v1/requests/REQ-9/audit-certificate",
               "signatures": [{"signedAt": "2026-06-28T03:00:00+00:00"}]}
    raw = json.dumps(payload).encode()
    out = await swr.sign_webhook(_request(raw, _sign(raw)), x_sign_signature=_sign(raw),
                                 session=session)
    assert out["received"] and out["contract_id"] == oc.id
    await session.flush()
    assert oc.sign_status == "COMPLETED" and oc.sign_envelope_id == "REQ-9"
    assert oc.sign_cert_serial == "AA:BB"
    assert oc.sign_doc_ref.endswith("audit-certificate") and oc.signed_at is not None
    # 멱등 — 재수신 동일값
    await swr.sign_webhook(_request(raw, _sign(raw)), x_sign_signature=_sign(raw),
                           session=session)
    await session.flush()
    assert oc.sign_status == "COMPLETED"


async def test_webhook_bad_signature_rejected(session, _secret):
    oc = await _oc(session)
    raw = json.dumps({"eventType": "request.completed", "externalDocId": f"oc:{oc.id}"}).encode()
    with pytest.raises(ForbiddenError, match="불일치"):
        await swr.sign_webhook(_request(raw, "wrong"), x_sign_signature="wrong", session=session)


async def test_webhook_non_completed_ignored(session, _secret):
    raw = json.dumps({"eventType": "request.viewed", "externalDocId": "oc:1"}).encode()
    out = await swr.sign_webhook(_request(raw, _sign(raw)), x_sign_signature=_sign(raw),
                                 session=session)
    assert out["received"] and out["ignored"] == "request.viewed"


async def test_webhook_non_oc_ref_noop(session, _secret):
    raw = json.dumps({"eventType": "request.completed",
                      "externalDocId": "erpcontract:medical:5", "requestId": "R"}).encode()
    out = await swr.sign_webhook(_request(raw, _sign(raw)), x_sign_signature=_sign(raw),
                                 session=session)
    assert "ignored" in out


async def test_webhook_unknown_contract_raises(session, _secret):
    raw = json.dumps({"eventType": "request.completed", "externalDocId": "oc:999999",
                      "requestId": "R"}).encode()
    with pytest.raises(ValidationError, match="위탁계약"):
        await swr.sign_webhook(_request(raw, _sign(raw)), x_sign_signature=_sign(raw),
                               session=session)


# ── 개시(initiate) 오케스트레이션 ─────────────────────────
@pytest.fixture
def _mock_sign(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "attachment_dir", str(tmp_path), raising=False)
    from erp.integrations import sign_client
    calls = {}

    async def _enroll(name):
        calls["enroll"] = name
        return "CERT-100"

    async def _create(**kw):
        calls["create"] = kw
        return "REQ-77"

    async def _tokens(rid):
        calls["tokens"] = rid
        return [{"participantId": "P1", "signerRole": "VENDOR", "token": "t",
                 "url": "/portal/t", "link": "https://sign.example.org/portal/t"}]

    monkeypatch.setattr(sign_client, "enroll_witness", _enroll)
    monkeypatch.setattr(sign_client, "create_portal_request", _create)
    monkeypatch.setattr(sign_client, "portal_tokens", _tokens)
    return calls


async def test_initiate_signing_flow(session, _mock_sign):
    oc = await _oc(session)
    out = await initiate_outsourcing_signing(
        session, actor=1, contract_id=oc.id, signer_name="대표이사",
        pdf_bytes=b"%PDF-1.4 contract", filename="c.pdf", content_type="application/pdf")
    await session.flush()
    assert out["request_id"] == "REQ-77" and out["status"] == "SENT"
    assert out["links"][0]["link"].startswith("https://sign.example.org")
    assert oc.sign_envelope_id == "REQ-77" and oc.sign_cert_serial == "CERT-100"
    assert oc.sign_doc_attachment_id is not None
    assert oc.sign_doc_ref == "sha256:" + hashlib.sha256(b"%PDF-1.4 contract").hexdigest()
    # sign에 보낸 documentHash = PDF sha256 (원본 미반출)
    assert _mock_sign["create"]["document_hash"] == hashlib.sha256(b"%PDF-1.4 contract").hexdigest()
    assert _mock_sign["create"]["external_doc_id"] == f"oc:{oc.id}"


async def test_initiate_resend_skips_enroll(session, _mock_sign):
    oc = await _oc(session, sign_envelope_id="REQ-EXIST", sign_status="SENT")
    out = await initiate_outsourcing_signing(
        session, actor=1, contract_id=oc.id, signer_name="대표",
        pdf_bytes=b"%PDF-1.4", filename="c.pdf", content_type="application/pdf")
    assert out["resent"] is True and out["request_id"] == "REQ-EXIST"
    assert "enroll" not in _mock_sign        # 재발급 안 함
    assert _mock_sign["tokens"] == "REQ-EXIST"


async def test_initiate_rejects_completed_and_non_pdf(session, _mock_sign):
    done = await _oc(session, sign_status="COMPLETED")
    with pytest.raises(ValidationError, match="완료"):
        await initiate_outsourcing_signing(session, actor=1, contract_id=done.id,
                                           signer_name="x", pdf_bytes=b"%PDF",
                                           filename="c.pdf", content_type="application/pdf")
    fresh = await _oc(session)
    with pytest.raises(ValidationError, match="PDF"):
        await initiate_outsourcing_signing(session, actor=1, contract_id=fresh.id,
                                           signer_name="x", pdf_bytes=b"x",
                                           filename="c.txt", content_type="text/plain")


async def test_webhook_timestamp_bound_scheme(session, _secret, monkeypatch):
    """#3 HIS 계약 — X-Sign-Timestamp + X-HIS-Signature-Ts=HMAC("{ts}.{raw}")+신선도 검증."""
    import time as _time

    from erp.errors import ForbiddenError
    oc = await _oc(session)
    payload = {"eventType": "request.completed", "requestId": "REQ-T",
               "externalDocId": f"oc:{oc.id}", "status": "COMPLETED"}
    raw = json.dumps(payload).encode()
    ts = str(int(_time.time()))
    ts_sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    out = await swr.sign_webhook(_request(raw, None), x_his_signature_ts=ts_sig,
                                 x_sign_timestamp=ts, session=session)
    assert out["received"]
    # 만료된 타임스탬프는 거부(재생 방지)
    old_ts = str(int(_time.time()) - 100000)
    old_sig = hmac.new(SECRET.encode(), f"{old_ts}.".encode() + raw, hashlib.sha256).hexdigest()
    with pytest.raises(ForbiddenError, match="만료"):
        await swr.sign_webhook(_request(raw, None), x_his_signature_ts=old_sig,
                               x_sign_timestamp=old_ts, session=session)
    # 구 스킴(timestamp 없음·X-Sign-Signature=rawBody)은 여전히 통과(하위호환)
    sig2 = _sign(raw)
    out2 = await swr.sign_webhook(_request(raw, sig2), x_sign_signature=sig2,
                                  x_sign_timestamp=None, session=session)
    assert out2["received"]
