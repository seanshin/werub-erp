"""공공 수가 마스터 REST 어댑터 (FR-34) — SSRF·파싱·FeeSyncService 브리지.

소비측(FeeSyncService.apply 멱등·close-then-open)은 test_fee_sync.py가 검증 — 여기선
어댑터 fetch·매핑·run_fee_sync 배선을 검증.
"""
from datetime import date

import httpx
import pytest
from sqlalchemy import select

from erp.integrations.fee_sync import RestFeeMasterSyncSource, run_fee_sync
from erp.regulatory.models import FeeMaster

ITEMS = [
    {"code": "AA100", "codeType": "1", "name": "진찰료", "unitPrice": 15000,
     "appliedAt": "20260701"},
    {"code": "D0001", "codeType": "2", "name": "아목시실린", "unitPrice": 320,
     "priceCap": 400, "appliedAt": "20260701"},
    {"name": "결손", "appliedAt": "20260701"},  # code 결손 → 스킵
]


def _source(items=ITEMS) -> RestFeeMasterSyncSource:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["serviceKey"] == "svc-key"   # data.go.kr 인증
        assert "fromDate" in request.url.params
        return httpx.Response(200, json={"response": {"body": {"items": items}}})

    return RestFeeMasterSyncSource(
        "https://apis.data.go.kr", "svc-key",
        allowed_hosts=frozenset({"apis.data.go.kr"}),
        transport=httpx.MockTransport(handler))


def test_ssrf_guard():
    with pytest.raises(ValueError, match="허용되지 않은 수가 API"):
        RestFeeMasterSyncSource("https://evil.example", "k")


def test_fetch_parses_and_maps():
    dtos = list(_source().fetch_updates(date(2026, 6, 24)))
    assert len(dtos) == 2                                        # 결손 1건 스킵
    assert dtos[0].code == "AA100" and dtos[0].code_type == "TARIFF"
    assert dtos[0].valid_from == date(2026, 7, 1)
    assert dtos[1].code_type == "DRUG" and dtos[1].price_cap == 400.0


async def test_run_fee_sync_applies_to_master(session):
    applied = await run_fee_sync(session, _source(),
                                 source_version="api-2026-06-24", since=date(2026, 6, 17))
    assert applied == 2
    rows = (await session.execute(select(FeeMaster).where(FeeMaster.valid_to.is_(None)))).scalars().all()
    assert {r.code for r in rows} == {"AA100", "D0001"}
    assert all(r.source == "API" for r in rows)


async def test_run_fee_sync_idempotent_by_source_version(session):
    await run_fee_sync(session, _source(), source_version="api-2026-06-24", since=date(2026, 6, 17))
    again = await run_fee_sync(session, _source(), source_version="api-2026-06-24",
                               since=date(2026, 6, 17))
    assert again == 0                                           # 동일 source_version no-op
    rows = (await session.execute(select(FeeMaster))).scalars().all()
    assert len(rows) == 2                                       # 중복 미생성
