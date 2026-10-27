"""공공 수가 마스터 동기화 어댑터 (FR-34) — design §3.2.

보건의료빅데이터개방시스템(data.go.kr, `apis.data.go.kr/B551182/...`)에서 행위·약제·
치료재료 단가 마스터를 수신해 Regulatory 마스터에 temporal 반영. 수납·환자와 달리
**스테이징 미경유** — FeeSyncService(멱등·close-then-open·이상변동 보류)가 안전 소비자.

⚠ 단가 마스터 API의 정확한 엔드포인트·응답 스키마는 미확정(M0b 화이트리스트와 함께 확정,
references/hira-edi-spec/nursing-grade-research.md §수가 공공 API). 경로·응답키는 가정값 —
계약 확정 시 이 어댑터만 교체. 호스트(apis.data.go.kr)·인증(serviceKey)·소비자는 불변.

보안: TLS verify 강제, 발신 도메인 화이트리스트(SSRF). data.go.kr은 Bearer가 아니라
serviceKey 쿼리 파라미터 인증.
"""
import logging
from collections.abc import Iterator
from datetime import date

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from erp.integrations.sources import RawFeeDTO
from erp.regulatory.sync import FeeSyncService, FeeUpdate

logger = logging.getLogger("erp.integrations.fee_sync")

ALLOWED_HOSTS = frozenset({"apis.data.go.kr"})  # SSRF 화이트리스트

# 공공 API 구분코드 → ERP code_type (가정 매핑, 계약 확정 시 보정)
_CODE_TYPE = {"1": "TARIFF", "2": "DRUG", "3": "MATERIAL",
              "TARIFF": "TARIFF", "DRUG": "DRUG", "MATERIAL": "MATERIAL"}


class RestFeeMasterSyncSource:
    """data.go.kr 단가 마스터 구현체 (FeeMasterSyncSource Protocol) — 동기(배치)."""

    def __init__(self, base_url: str, service_key: str,
                 path: str = "/B551182/feeMasterService/getFeeList",
                 timeout: float = 30.0, allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
                 transport: httpx.BaseTransport | None = None):
        host = httpx.URL(base_url).host
        if host not in allowed_hosts:
            raise ValueError(f"허용되지 않은 수가 API 대상: {host}")  # SSRF 방어
        self._base = base_url.rstrip("/")
        self._path = path
        self._service_key = service_key
        self._timeout = timeout
        self._transport = transport

    def fetch_updates(self, since: date) -> Iterator[RawFeeDTO]:
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            resp = client.get(f"{self._base}{self._path}", params={
                "serviceKey": self._service_key, "type": "json",
                "fromDate": since.strftime("%Y%m%d"), "numOfRows": 10000})
            resp.raise_for_status()
            body = ((resp.json().get("response") or {}).get("body") or {})
        for item in body.get("items") or []:
            dto = self._to_dto(item)
            if dto is not None:
                yield dto

    @staticmethod
    def _to_dto(item: dict) -> RawFeeDTO | None:
        code = item.get("code") or item.get("cd")
        applied = item.get("appliedAt") or item.get("mdfcDd")  # 적용일 YYYYMMDD
        if not code or not applied:
            return None
        price = item.get("unitPrice") if item.get("unitPrice") is not None else item.get("amt")
        cap = item.get("priceCap")
        return RawFeeDTO(
            code=str(code), code_type=_CODE_TYPE.get(str(item.get("codeType", "1")), "TARIFF"),
            name=item.get("name") or item.get("cdNm") or "",
            unit_price=float(price or 0),
            price_cap=float(cap) if cap is not None else None,
            valid_from=date(int(applied[:4]), int(applied[4:6]), int(applied[6:8])))


async def run_fee_sync(session: AsyncSession, source, *, source_version: str,
                       since: date, force: bool = False) -> int:
    """어댑터 → FeeSyncService.apply 브리지. 반환: 반영된 행 수."""
    updates = [
        FeeUpdate(code=d.code, code_type=d.code_type, name=d.name,
                  unit_price=d.unit_price, price_cap=d.price_cap, valid_from=d.valid_from)
        for d in source.fetch_updates(since)
    ]
    applied = await FeeSyncService().apply(
        session, source_version=source_version, updates=updates, force=force)
    logger.info("수가 동기화", extra={"ctx": {
        "source_version": source_version, "fetched": len(updates), "applied": applied}})
    return applied
