"""ERP→HIS 수가/재료비 push (ERP=마스터 → HIS 소비). HIS 인바운드 EP로 배열 일괄 upsert.

인증 X-Integration-Key(공유 연동키), SSRF 화이트리스트 his.example.org, TLS verify.
EP: /api/v1/integration/fee/{procedure-codes|non-covered|material-costs}.
"""
from urllib.parse import urlparse

import httpx

from erp.integrations.egress import allowed_hosts as _egress_hosts



class HISFeePushClient:
    def __init__(self, base_url: str, integration_key: str, timeout: float = 60.0,
                 allowed_hosts: frozenset[str] | None = None,
                 transport: httpx.BaseTransport | None = None):
        if urlparse(base_url).hostname not in (allowed_hosts or _egress_hosts("his")):
            raise ValueError(f"허용되지 않은 대상: {urlparse(base_url).hostname}")
        self._base = base_url.rstrip("/")
        self._key = integration_key
        self._timeout = timeout
        self._transport = transport

    def _post(self, path: str, items: list[dict]) -> dict:
        with httpx.Client(timeout=self._timeout, transport=self._transport, verify=True) as c:
            r = c.post(f"{self._base}{path}", json={"items": items},
                       headers={"X-Integration-Key": self._key})
            r.raise_for_status()
            body = r.json()
            return body.get("data", body)

    def push_procedure_codes(self, items: list[dict]) -> dict:
        return self._post("/api/v1/integration/fee/procedure-codes", items)

    def push_non_covered(self, items: list[dict]) -> dict:
        return self._post("/api/v1/integration/fee/non-covered", items)

    def push_material_costs(self, items: list[dict]) -> dict:
        return self._post("/api/v1/integration/fee/material-costs", items)
