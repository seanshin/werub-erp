"""안전재고 자동발주(par-level 보충·P3-7) — 재고 < 안전재고(재주문점) 품목을 par_level까지 보충 제안.

제안 수량 = ceil(effective_par − 현재고), effective_par = par_level(미설정 시 안전재고×2 기본 버퍼).
선택 품목을 1건 구매요청(PR)으로 일괄 생성(기존 PurchaseRequisitionService 재사용). 발주·승인은 PR 흐름.
"""
import math
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.scm.models import Item, Lot
from erp.scm.requisition_service import PurchaseRequisitionService


class ReplenishmentService:
    def __init__(self) -> None:
        self._pr = PurchaseRequisitionService()

    def _effective_par(self, item: Item) -> float:
        if item.par_level is not None and float(item.par_level) > 0:
            return float(item.par_level)
        return float(item.safety_stock_qty) * 2      # 기본: 안전재고 2배까지 보충

    async def _stock_map(self, session: AsyncSession, item_ids: list[int]) -> dict[int, float]:
        if not item_ids:
            return {}
        rows = (await session.execute(
            select(Lot.item_id, func.coalesce(func.sum(Lot.remaining_qty), 0))
            .where(Lot.item_id.in_(item_ids)).group_by(Lot.item_id))).all()
        return {iid: float(q) for iid, q in rows}

    async def suggestions(self, session: AsyncSession) -> dict:
        """재주문점(안전재고) 미만 품목 + 보충 제안 수량. 안전재고>0 품목만 대상."""
        items = (await session.scalars(select(Item).where(Item.safety_stock_qty > 0))).all()
        stock = await self._stock_map(session, [it.id for it in items])
        out = []
        for it in items:
            cur = stock.get(it.id, 0.0)
            if cur >= float(it.safety_stock_qty):
                continue                               # 재주문점 이상 — 발주 불필요
            par = self._effective_par(it)
            suggested = max(0, math.ceil(par - cur))
            if suggested <= 0:
                continue
            out.append({"item_id": it.id, "code": it.code, "name": it.name, "unit": it.unit,
                        "current": round(cur, 2), "safety_stock": float(it.safety_stock_qty),
                        "par_level": float(it.par_level) if it.par_level is not None else None,
                        "effective_par": par, "suggested_qty": suggested,
                        "is_narcotic": it.is_narcotic})
        out.sort(key=lambda r: (r["current"] / (r["safety_stock"] or 1)))   # 결핍률 높은 순
        return {"as_of": str(date.today()), "count": len(out), "items": out}

    async def create_pr(self, session: AsyncSession, *, item_ids: list[int], requester: int,
                        department_id: int | None = None,
                        qty_override: dict[int, float] | None = None):
        """선택 품목을 1건 PR로 생성 — 제안 수량(또는 override) 사용. 재주문점 이상 품목은 거부."""
        if not item_ids:
            raise ValidationError("보충할 품목을 선택하세요")
        sug = {s["item_id"]: s for s in (await self.suggestions(session))["items"]}
        override = qty_override or {}
        lines = []
        for iid in dict.fromkeys(item_ids):
            s = sug.get(iid)
            qty = override.get(iid, s["suggested_qty"] if s else None)
            if qty is None:
                raise ValidationError(f"보충 대상이 아니거나 수량 미상: {iid}")
            if float(qty) <= 0:
                continue
            lines.append({"item_id": iid, "qty": float(qty),
                          "note": "안전재고 보충(자동제안)"})
        if not lines:
            raise ValidationError("보충 수량이 유효한 품목이 없습니다")
        return await self._pr.create(session, requester=requester, department_id=department_id,
                                     lines=lines, reason="안전재고 자동발주 보충")
