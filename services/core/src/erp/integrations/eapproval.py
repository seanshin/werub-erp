"""전자결재(HIS W.Sign 릴레이) — 상신 클라이언트 + 콜백 HMAC 검증.

보안(기존 ACL 어댑터 동일): TLS verify 강제, 도메인 화이트리스트(SSRF), X-Integration-Key 인증.
콜백은 X-Signature = sha256=HMAC-SHA256(body, EAPPROVAL_CALLBACK_SECRET) 검증.
"""
import hashlib
import hmac
from urllib.parse import urlparse

import httpx

from erp.integrations.egress import allowed_hosts as _egress_hosts



def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """콜백 서명 검증 — 상수시간 비교. signature 형식: 'sha256=<hex>'."""
    if not signature or not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def callback_age_seconds(payload: dict, *, now_epoch: float) -> float | None:
    """서명 본문의 발급 시각 기준 경과 초 — `ts`(epoch) 우선, 없으면 HIS 정본 `decided_at`(ISO)."""
    ts = payload.get("ts")
    if ts is not None:
        try:
            return now_epoch - float(ts)
        except (TypeError, ValueError):
            return None
    iso = payload.get("decided_at") or payload.get("decidedAt")
    if not isinstance(iso, str):
        return None
    from datetime import datetime
    try:
        return now_epoch - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def callback_replay_reason(payload: dict, *, now_epoch: float, max_skew: int) -> str | None:
    """콜백 재생(replay) 방지 — 서명 본문의 발급시각 검사. 통과 시 None, 거부 시 사유.

    - `ts`(epoch초)를 실은 발신자: 종전 계약대로 max_skew 신선도 강제.
    - HIS 정본 계약(eapproval-callback.ts — 본문에 `ts` 없음·`decided_at` ISO): 시각이 있고 읽히면
      통과. **늦게 온 것은 거부하지 않는다** — HIS는 즉시 3회 실패분을 아웃박스로 나중에 재전달하고
      (같은 본문·같은 서명), 그것을 신선도로 끊으면 결재 결과가 ERP에 영영 닿지 않는다. 종전엔 `ts`
      누락으로 HIS 콜백 **전부**를 거부했다.
      재생 방어는 HMAC(위조 불가) + event_id 1회 소비(라우터) + REQUESTED에서만 전이하는 멱등
      처리가 맡는다 — 진짜 결재 결과를 다시 적용해도 상태는 바뀌지 않는다.
    - 둘 다 없거나 읽을 수 없으면 거부(검사 우회 차단).
    (event_id 단일소비는 라우터 nonce 계층이 담당)
    """
    if payload.get("ts") is not None:
        age = callback_age_seconds(payload, now_epoch=now_epoch)
        if age is None:
            return "ts 형식 오류"
        if abs(age) > max_skew:
            return "신선도 창 초과(재생 의심)"
        return None
    if callback_age_seconds(payload, now_epoch=now_epoch) is None:
        return "발급시각 누락(ts 또는 decided_at 필요·재생 방지)"
    return None


class EApprovalClient:
    def __init__(self, base_url: str, integration_key: str, hospital_code: str,
                 timeout: float = 10.0, allowed_hosts: frozenset[str] | None = None,
                 transport: httpx.BaseTransport | None = None):
        host = urlparse(base_url).hostname
        if host not in (allowed_hosts or _egress_hosts("his")):
            raise ValueError(f"허용되지 않은 전자결재 대상: {host}")  # SSRF 방어
        self._base = base_url.rstrip("/")
        self._key = integration_key
        self._hospital_code = hospital_code
        self._timeout = timeout
        self._transport = transport

    def _headers(self, idem: str | None = None) -> dict:
        # HIS ERP-facing 엔드포인트 확정 스펙: X-Integration-Key만 필수(X-Hospital-Code 미사용)
        h = {"X-Integration-Key": self._key}
        if idem:
            h["Idempotency-Key"] = idem
        return h

    @staticmethod
    def _unwrap(data: dict) -> dict:
        """HIS 표준 data 봉투 언랩 — {"data": {...}} → {...} (없으면 원본)."""
        return data["data"] if isinstance(data, dict) and isinstance(data.get("data"), dict) else data

    def submit(self, payload: dict, *, path: str) -> dict:
        """결재 상신 릴레이 — HIS가 W.Sign으로 포워드. 응답(data 봉투 언랩): approval_id·view_url 등."""
        ref = payload.get("ref", {})
        idem = f"{ref.get('source_module')}:{ref.get('source_ref')}"
        with httpx.Client(timeout=self._timeout, transport=self._transport, verify=True) as c:
            r = c.post(f"{self._base}{path}", json=payload, headers=self._headers(idem))
            r.raise_for_status()
            return self._unwrap(r.json())

    def get_status(self, approval_id: str, *, path: str) -> dict:
        with httpx.Client(timeout=self._timeout, transport=self._transport, verify=True) as c:
            r = c.get(f"{self._base}{path}/{approval_id}", headers=self._headers())
            r.raise_for_status()
            return self._unwrap(r.json())
