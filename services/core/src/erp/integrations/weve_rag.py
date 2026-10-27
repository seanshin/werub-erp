"""example.org AI 사전심사 — 공시 RAG 검색 클라이언트 (P0 Retrieval).

`POST {base}/api/notice/search` — 색인된 공시(notice_knowledge) 검색 + 필터 3종
(notice_type·effective_on 진료일 시행여부·active_only 폐지제외).
보안: SSRF 화이트리스트 example.org·TLS·Bearer. 미설정/오류 시 호출부가 로컬 검색 fallback.
"""
from urllib.parse import urlparse

import httpx

from erp.integrations.egress import allowed_hosts as _egress_hosts



class WeveRagClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0,
                 transport: httpx.BaseTransport | None = None):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname not in _egress_hosts("ai"):
            raise ValueError(f"허용되지 않은 대상: {parsed.scheme}://{parsed.hostname}")
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self._transport = transport

    def search(self, query: str, *, n_results: int = 5,
               notice_type: list[str] | None = None, effective_on: str | None = None,
               active_only: bool = True) -> list[dict]:
        """공시 검색 — 진료일 시행여부·코드type·폐지제외 필터. 결과 [{text, score, metadata}]."""
        body: dict = {"query": query, "n_results": n_results, "active_only": active_only}
        if notice_type:
            body["notice_type"] = notice_type
        if effective_on:
            body["effective_on"] = effective_on
        with httpx.Client(timeout=self._timeout, transport=self._transport, verify=True) as c:
            r = c.post(f"{self._base}/api/notice/search", json=body,
                       headers={"Authorization": f"Bearer {self._key}"})
            r.raise_for_status()
            data = r.json()
        return (data.get("results") or data.get("items")
                or data.get("data", {}).get("results") or [])

    def diagnose(self, *, code: str, description: str | None = None,
                 service_date: str | None = None, context: str | None = None,
                 notice_type: list[str] | None = None) -> dict:
        """삭감위험 평가 (P0.5/P1) — 청구라인(비식별) → 위험도·근거 인용. 근거 없으면 보류 신호."""
        claim = {"code": code}
        if description:
            claim["description"] = description
        if service_date:
            claim["service_date"] = service_date
        if context:
            claim["context"] = context
        body: dict = {"claim_line": claim}
        if notice_type:
            body["notice_type"] = notice_type
        with httpx.Client(timeout=self._timeout, transport=self._transport, verify=True) as c:
            r = c.post(f"{self._base}/api/notice/diagnose", json=body,
                       headers={"Authorization": f"Bearer {self._key}"})
            r.raise_for_status()
            data = r.json()
        return data.get("data", data)
