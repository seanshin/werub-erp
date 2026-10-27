"""e-sign 트랙 A 클라이언트 (ERP→sign 직접) — 외부 거래처 계약 서명.

확정 핸드오프(seanshin/sign:docs/05-handoff/erp-track-a-handoff.md): enroll(WITNESS) →
requests(PORTAL·NONE) → portal-tokens. 동일 서버 내부 호출(http://127.0.0.1:4100/v1, x-api-key).
경량 본인인증(requiredIdentityLevel=NONE). ERP는 계약 PDF SHA-256만 송신(원본 미반출).
"""
import hashlib

import httpx

from erp.config import get_settings
from erp.errors import ValidationError


def pdf_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _client() -> httpx.AsyncClient:
    s = get_settings()
    if not s.sign_api_key:
        raise ValidationError("sign 연동키 미설정 (SIGN_API_KEY)")
    return httpx.AsyncClient(base_url=s.sign_internal_base, timeout=15.0,
                             headers={"x-api-key": s.sign_api_key,
                                      "Content-Type": "application/json"})


async def enroll_witness(subject_name: str) -> str:
    """외부 서명자(거래처 대표) 인증서 발급(WITNESS·1일 단기). serialNumber 반환."""
    async with _client() as c:
        r = await c.post("/certificates/enroll",
                         json={"ownerType": "WITNESS", "subjectDn": f"CN={subject_name}"})
        r.raise_for_status()
        return r.json()["data"]["serialNumber"]


async def create_portal_request(*, idempotency_key: str, external_doc_id: str, title: str,
                                document_hash: str, callback_url: str, signer_name: str,
                                cert_serial: str) -> str:
    """포털 서명요청 생성(경량 NONE). requestId 반환. documentHash=계약 PDF SHA-256."""
    async with _client() as c:
        r = await c.post("/requests", json={
            "idempotencyKey": idempotency_key, "externalDocId": external_doc_id, "title": title,
            "documentHash": document_hash, "signMode": "PORTAL", "callbackUrl": callback_url,
            "participants": [{"signerRole": "VENDOR", "signerType": "WITNESS_EXTERNAL",
                              "displayName": signer_name, "signOrder": 1,
                              "certSerial": cert_serial, "requiredIdentityLevel": "NONE"}]})
        r.raise_for_status()
        return r.json()["data"]["requestId"]


async def portal_tokens(request_id: str) -> list[dict]:
    """참가자별 1회용 포털 토큰(deep-link). 거래처 전달 링크 = sign_portal_base + url."""
    s = get_settings()
    async with _client() as c:
        r = await c.post(f"/requests/{request_id}/portal-tokens")
        r.raise_for_status()
        out = r.json()["data"]
    for t in out:
        t["link"] = s.sign_portal_base + t["url"]   # 거래처 발송용 공개 링크
    return out


# ── sign 블록체인 인증 로그(/v1/audit-events) — 자금결재 원장 앵커 시점봉인(B안) ──────────
# PHI·원본 금지(sign 계약): 식별자·해시·집계만 제출. 트랙A와 동일 키(requests:write 스코프).
FUND_APPROVAL_STREAM = "erp-fund-approvals"


ERP_AUDIT_ACTOR = "erp-system:approval-ledger"   # 시스템 주장(API_KEY) 행위자 — 사람 아님


async def record_audit_event(*, stream: str, action: str, target_type: str, target_ref: str,
                             data: dict, anchor: bool = False,
                             actor: str = ERP_AUDIT_ACTOR) -> dict:
    """행위 기록 — sign append-only 해시체인 봉인(+anchor=True면 TSA 시점봉인). 인증 영수증 반환.

    ⚠ sign AuditEventDto는 `actor`가 **필수**다. 종전 본문엔 actor가 없어 매번 400이었고, 봉인은
    v1.259.0 이래 한 번도 성립하지 않았다.
    """
    async with _client() as c:
        r = await c.post("/audit-events", json={
            "stream": stream, "action": action, "actor": actor, "targetType": target_type,
            "targetRef": target_ref, "data": data, "anchor": anchor})
        r.raise_for_status()
        return r.json()["data"]


async def list_audit_events(*, stream: str, limit: int = 20, offset: int = 0) -> dict:
    """스트림 행위 로그 조회 — 테넌트 격리(ERP consumerApp 자기 기록만). {data, page}."""
    async with _client() as c:
        r = await c.get("/audit-events", params={"stream": stream, "limit": limit,
                                                 "offset": offset})
        r.raise_for_status()
        return r.json()


async def latest_audit_event(*, stream: str, event_type: str) -> dict | None:
    """스트림에서 event_type의 **가장 최근** 항목 — 없으면 None.

    sign 목록은 seq **오름차순**(오래된 것 먼저)이고 항목의 유형 필드는 `eventType`이다. 종전엔
    첫 페이지 20건에서 `action`으로 걸러 `[0]`을 최신으로 썼다 — 필드명이 달라 늘 0건(=봉인
    대조·지연 경보 무음)이었고, 맞았더라도 가장 **오래된** 봉인을 대조했을 것이다.
    eventType 서버 필터로 총건수를 얻고 마지막 1건을 다시 읽는다.
    """
    async with _client() as c:
        params = {"stream": stream, "eventType": event_type, "limit": 1, "offset": 0}
        r = await c.get("/audit-events", params=params)
        r.raise_for_status()
        total = int(((r.json().get("page") or {}).get("total")) or 0)
        if total == 0:
            return None
        if total > 1:
            r = await c.get("/audit-events", params={**params, "offset": total - 1})
            r.raise_for_status()
        items = r.json().get("data") or []
        return items[-1] if items else None


async def verify_audit_chain(*, stream: str) -> dict:
    """sign측 체인 무결성 + 앵커(TSA) 검증 결과."""
    async with _client() as c:
        r = await c.get("/audit-events/verify", params={"stream": stream})
        r.raise_for_status()
        return r.json()["data"]
