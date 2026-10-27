"""ERP fee_master → HIS push. 수가(TARIFF)→procedure-codes, 치료재료(MATERIAL)→non-covered.

이력 보존: (code, validFrom) 단위로 전송 — HIS가 복합키 보존 시 과거 청구 재현 가능.
사용: python scripts/push_fees_to_his.py [TARIFF|MATERIAL|ALL] [limit] [batch]
대량(수가 41만)은 배치 분할. 멱등(HIS upsert)이라 재실행 안전.
"""
import asyncio
import sys

import httpx
from sqlalchemy import select

from erp.config import get_settings
from erp.db import session_factory
from erp.integrations.his_fee_push import HISFeePushClient
from erp.regulatory.models import FeeMaster


def _proc_item(f: FeeMaster) -> dict:
    return {"code": f.code, "nameKo": f.name, "unitPrice": int(f.unit_price or 0),
            "insuranceCovered": True, "validFrom": f.valid_from.isoformat(),
            "validTo": f.valid_to.isoformat() if f.valid_to else None}


def _nc_item(f: FeeMaster) -> dict:
    return {"code": f.code, "name": f.name, "category": "MATERIAL",
            "price": int(f.price_cap or f.unit_price or 0),
            "description": (f.criteria_text or None)}


async def _send(push, chunk: list[dict], retries: int = 8):
    """전송 — HIS 스로틀(Too Many Requests)은 백오프 재시도, 그 외는 실패 반환."""
    for attempt in range(retries):
        try:
            return await asyncio.to_thread(push, chunk), None
        except httpx.HTTPStatusError as exc:
            body = exc.response.text if exc.response is not None else ""
            throttled = ("Throttler" in body or "Too Many" in body
                         or (exc.response is not None and exc.response.status_code == 429))
            if throttled and attempt < retries - 1:
                await asyncio.sleep(1.0 + attempt * 1.0)   # 점증 백오프
                continue
            return None, exc
        except Exception as exc:  # noqa: BLE001
            return None, exc
    return None, RuntimeError("throttle 재시도 소진")


async def _push_chunk(push, chunk, acc):
    """배치 전송 — 실패 시 50건→1건으로 세분화해 불량 레코드 격리(끝까지 진행)."""
    res, err = await _send(push, chunk)
    if err is None:
        for k in ("created", "updated", "failed"):
            acc[k] += res.get(k, 0)
        return
    if len(chunk) == 1:
        acc["failed"] += 1
        acc["bad"].append(chunk[0]["code"])
        return
    step = 50 if len(chunk) > 50 else 1
    for j in range(0, len(chunk), step):
        await _push_chunk(push, chunk[j:j + step], acc)


async def _push_type(client, code_type: str, limit: int, batch: int) -> dict:
    push = client.push_procedure_codes if code_type == "TARIFF" else client.push_non_covered
    to_item = _proc_item if code_type == "TARIFF" else _nc_item
    async with session_factory()() as s:
        stmt = select(FeeMaster).where(FeeMaster.code_type == code_type)
        if code_type == "TARIFF":            # procedure-codes는 단가 필수 — 0단가(비적용)는 제외
            stmt = stmt.where(FeeMaster.unit_price > 0)
        stmt = stmt.order_by(FeeMaster.id)
        if limit:
            stmt = stmt.limit(limit)
        rows = (await s.execute(stmt)).scalars().all()
    acc = {"created": 0, "updated": 0, "failed": 0, "bad": []}
    sent = 0
    for i in range(0, len(rows), batch):
        chunk = [to_item(f) for f in rows[i:i + batch]]
        await _push_chunk(push, chunk, acc)
        sent += len(chunk)
        await asyncio.sleep(0.1)             # 요청 간 소폭 지연(스로틀 해제됨)
        if (i // batch) % 20 == 0 or sent >= len(rows):
            print(f"  {code_type} {sent}/{len(rows)} "
                  f"(created {acc['created']} updated {acc['updated']} failed {acc['failed']})")
    if acc["bad"]:
        print(f"  ⚠️ 불량 코드 {len(acc['bad'])}건(예): {acc['bad'][:20]}")
    return {"type": code_type, "sent": sent,
            **{k: acc[k] for k in ("created", "updated", "failed")}, "bad_count": len(acc["bad"])}


async def main(which: str, limit: int, batch: int) -> None:
    s = get_settings()
    # fee EP는 공유 연동키(HIS_WEBHOOK_KEY=hiserp_) 사용 — erpik_은 eapproval 전용
    key = s.his_webhook_key or s.his_integration_key
    if not key:
        print("연동 키 미설정 (HIS_WEBHOOK_KEY)")
        return
    client = HISFeePushClient(s.his_eapproval_base, key)
    # ALL=수가만(비급여/재료대는 ERP에 적정 price 데이터 부재 — 별도 소스 필요). MATERIAL은 명시 시만.
    types = ["TARIFF"] if which == "ALL" else [which]
    for t in types:
        print(f"=== push {t} (limit={limit or 'all'}, batch={batch}) ===")
        print(await _push_type(client, t, limit, batch))


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "ALL"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    batch = int(sys.argv[3]) if len(sys.argv) > 3 else 500
    asyncio.run(main(which, limit, batch))
