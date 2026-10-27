"""ERP→HIS material-costs push — 품목별 최신 매입단가(PoLine) → HIS 재고/CSSD 원가 갱신.

HIS 인바운드: POST /api/v1/integration/fee/material-costs {items:[{erpItemCode, unitPrice}]}
→ {total, inventoryUpdated, cssdUpdated, unmatched}. 키=공유 연동키(hiserp_). 멱등 upsert.
매입단가 = 품목별 가장 최근 발주라인(po.requested_at DESC) unit_price.

사용: python scripts/push_material_costs.py [limit=0(전체)] [batch=1000]
"""
import asyncio
import sys

from sqlalchemy import text

from erp.config import get_settings
from erp.db import session_factory
from erp.integrations.his_fee_push import HISFeePushClient


async def main(limit: int = 0, batch: int = 1000) -> None:
    s = get_settings()
    key = s.his_webhook_key or s.his_integration_key
    if not key:
        print("HIS 연동키 미설정 — 중단")
        return
    client = HISFeePushClient(s.his_api_base, key)
    async with session_factory()() as sess:
        rows = (await sess.execute(text(
            "SELECT DISTINCT ON (pl.item_id) i.code, pl.unit_price "
            "FROM scm.po_lines pl JOIN scm.items i ON i.id = pl.item_id "
            "JOIN scm.purchase_orders po ON po.id = pl.po_id "
            "ORDER BY pl.item_id, po.requested_at DESC, pl.id DESC"))).all()
    items = [{"erpItemCode": c, "unitPrice": int(p)} for c, p in rows if p and int(p) > 0]
    if limit:
        items = items[:limit]
    print(f"매입단가 보유 품목: {len(items)}")
    if not items:
        print("push할 매입단가 없음 — 발주(PO) 이력 필요")
        return
    total = {"total": 0, "inventoryUpdated": 0, "cssdUpdated": 0, "unmatched": 0}
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        res = await asyncio.to_thread(client.push_material_costs, chunk)
        d = res.get("data", res) if isinstance(res, dict) else {}
        for k in total:
            total[k] += int(d.get(k, 0) or 0)
        print(f"  {i + len(chunk)}/{len(items)}: {d}")
    print("결과:", total)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 0,
                     int(sys.argv[2]) if len(sys.argv) > 2 else 1000))
