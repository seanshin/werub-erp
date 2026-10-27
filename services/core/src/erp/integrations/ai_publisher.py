"""AI 색인 발신 어댑터 (FR-52) — 연동 스펙 v1.0 (notice-integration-reply-from-ai.md).

- 아웃바운드 전용, 폴링 단독 모드 (callback 없음)
- TLS 검증 강제 (verify 비활성화 금지 — NFR 외부 호출 보안)
- 도메인 화이트리스트 상수 한정 (SSRF 차단), 프록시는 httpx가 HTTPS_PROXY env 자동 적용
"""
import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx

from erp.integrations.egress import allowed_hosts as _egress_hosts

logger = logging.getLogger("erp.integrations.ai")



@dataclass
class NoticeEvent:
    event_type: str            # CREATE | UPDATE | REVOKE
    erp_ref_id: str
    revision_no: int
    client_request_id: str
    payload: dict | None       # REVOKE는 None (본문 미발신)


@dataclass
class PublishResult:
    ok: bool
    status_code: int
    elapsed_ms: int


class AIIndexPublisher(Protocol):
    async def publish(self, event: NoticeEvent) -> PublishResult: ...
    async def poll_status(self, client_request_id: str) -> str: ...
    async def sync_verify(self, snapshot: list[dict]) -> list[dict]: ...


def parse_index_status(data: dict) -> str:
    """index-status 응답 → 상태 문자열.

    서버 실응답(2026-07-03 확인)은 최상위 status가 아니라 `latest`(+`jobs[].status`)에 상태를
    담는다: {"client_request_id": …, "jobs": [{…, "status": "completed"}], "latest": "completed"}.
    기존 `.get("status")`는 키 부재로 항상 "pending" → 폴러가 영구 미종결(22일 재폴링 사고).
    latest → status → jobs 마지막 status 순으로 탐색, 전부 없으면 "pending".
    """
    status = data.get("latest") or data.get("status")
    if not status:
        jobs = data.get("jobs") or []
        if jobs and isinstance(jobs[-1], dict):
            status = jobs[-1].get("status")
    return status or "pending"


class HttpAIIndexPublisher:
    """example.org 구현체 — base: https://example.org/ollama/api/notice (스펙 v1.0 ③)."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        host = httpx.URL(base_url).host
        if host not in _egress_hosts("ai"):   # SSRF 화이트리스트 — 사용자 입력으로 URL 결정 금지
            raise ValueError(f"허용되지 않은 발신 대상: {host}")  # SSRF 방어
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout  # httpx 기본 verify=True — 끄지 않는다

    async def publish(self, event: NoticeEvent) -> PublishResult:
        async with httpx.AsyncClient(headers=self._headers, timeout=self._timeout) as client:
            if event.event_type == "REVOKE":
                resp = await client.post(f"{self._base}/{event.erp_ref_id}/revoke",
                                         json={"client_request_id": event.client_request_id})
            elif event.event_type == "UPDATE":
                resp = await client.put(f"{self._base}/{event.erp_ref_id}", json=event.payload)
            else:
                resp = await client.post(self._base, json=event.payload)
            return PublishResult(ok=resp.is_success, status_code=resp.status_code,
                                 elapsed_ms=int(resp.elapsed.total_seconds() * 1000))

    async def poll_status(self, client_request_id: str) -> str:
        """색인 상태 문자열 — `parse_index_status`로 latest/jobs[].status 추출(서버 실구현).

        스펙 v1.0은 pending/running/done/failed였으나 서버는 completed 등을 latest에 반환 →
        파싱은 `parse_index_status` 참조. 상태 정규화·종결 판정은 워커(ai_dispatch)가 담당."""
        async with httpx.AsyncClient(headers=self._headers, timeout=self._timeout) as client:
            resp = await client.get(f"{self._base}/index-status",
                                    params={"client_request_id": client_request_id})
            resp.raise_for_status()
            return parse_index_status(resp.json())

    async def sync_verify(self, snapshot: list[dict]) -> list[dict]:
        """주 1회 정합성 대사 — ERP push 방식 (스펙 v1.0 ⑥).

        요청 키는 정본 `snapshot`, 응답은 정본 `mismatched`. 종전 ERP는
        `items`/`mismatches`를 써서 AI Server(2026-10-07 이전)가 400 `snapshot array required`로
        거부했다. AI Server는 이후 양쪽을 수용하지만 정본으로 맞추고, 응답은 둘 다 읽는다.
        """
        async with httpx.AsyncClient(headers=self._headers, timeout=60.0) as client:
            resp = await client.post(f"{self._base}/sync/verify", json={"snapshot": snapshot})
            resp.raise_for_status()
            body = resp.json()
            out = body.get("mismatched")
            return out if isinstance(out, list) else body.get("mismatches", [])


def make_client_request_id() -> str:
    return str(uuid.uuid4())
