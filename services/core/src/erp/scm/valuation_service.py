"""재고자산 평가·안전재고 부족 리포트 (FR-13/46 확장).

평가액 = Σ(로트 잔량 × 매입단가). 단가 미상(과거 입고)은 0 처리(누락 표시). 안전재고 부족 =
품목 현재고(잔량 합) < safety_stock_qty.
"""
from calendar import monthrange
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.scm.models import Item, Lot, StockMovement

ACC_INV_ALLOWANCE = "1159"   # 재고자산평가충당금(자산 차감)
ACC_INV_LOSS = "5540"        # 재고자산평가손실


def _month_end(yyyymm: str) -> date:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return date(y, m, monthrange(y, m)[1])


class ValuationService:
    async def valuation(self, session: AsyncSession) -> dict:
        """품목별 재고자산 평가 — 잔량·평가액. 카테고리 소계·총계·단가누락 표시."""
        rows = (await session.execute(
            select(Item.id, Item.code, Item.name, Item.category,
                   func.coalesce(func.sum(Lot.remaining_qty), 0).label("qty"),
                   func.coalesce(func.sum(Lot.remaining_qty * Lot.unit_cost), 0).label("value"),
                   func.count(Lot.id).filter(Lot.unit_cost.is_(None)).label("no_cost"))
            .join(Lot, Lot.item_id == Item.id)
            .where(Lot.remaining_qty > 0)
            .group_by(Item.id, Item.code, Item.name, Item.category)
            .order_by(func.sum(Lot.remaining_qty * Lot.unit_cost).desc()))).all()
        items = [{"item_id": i, "code": c, "name": n, "category": cat,
                  "qty": float(q), "value": float(v), "cost_missing": int(nc) > 0}
                 for i, c, n, cat, q, v, nc in rows]
        by_cat: dict = {}
        for it in items:
            by_cat[it["category"]] = by_cat.get(it["category"], 0.0) + it["value"]
        return {"items": items, "total_value": sum(it["value"] for it in items),
                "by_category": [{"category": k, "value": v} for k, v in sorted(by_cat.items())],
                "missing_cost_items": sum(1 for it in items if it["cost_missing"])}

    async def shortage(self, session: AsyncSession) -> dict:
        """안전재고 미달 품목 — 현재고 < 안전재고."""
        rows = (await session.execute(
            select(Item.id, Item.code, Item.name, Item.category, Item.safety_stock_qty,
                   func.coalesce(
                       select(func.coalesce(func.sum(Lot.remaining_qty), 0))
                       .where(Lot.item_id == Item.id, Lot.remaining_qty > 0)
                       .scalar_subquery(), 0).label("on_hand"))
            .where(Item.safety_stock_qty > 0))).all()
        out = []
        for i, c, n, cat, safety, on_hand in rows:
            oh, ss = float(on_hand), float(safety)
            if oh < ss:
                out.append({"item_id": i, "code": c, "name": n, "category": cat,
                            "on_hand": oh, "safety_stock": ss, "shortage": ss - oh})
        return {"count": len(out), "items": sorted(out, key=lambda x: -x["shortage"])}

    async def turnover(self, session: AsyncSession, *, since: date, until: date) -> dict:
        """재고회전율 — 기간 출고량/현재고, 회전일수, 장기 미사용(dead stock) 탐지."""
        days = max(1, (until - since).days + 1)
        out_rows = (await session.execute(
            select(StockMovement.item_id,
                   func.coalesce(func.sum(func.abs(StockMovement.qty)), 0))
            .where(StockMovement.movement_type.in_(("OUT", "DISPOSE")),
                   StockMovement.moved_on >= since, StockMovement.moved_on <= until)
            .group_by(StockMovement.item_id))).all()
        out_map = {i: float(q) for i, q in out_rows}
        stock_rows = (await session.execute(
            select(Item.id, Item.code, Item.name, Item.category,
                   func.coalesce(func.sum(Lot.remaining_qty), 0))
            .join(Lot, Lot.item_id == Item.id).where(Lot.remaining_qty > 0)
            .group_by(Item.id, Item.code, Item.name, Item.category))).all()
        items, dead = [], 0
        for iid, code, name, cat, stock in stock_rows:
            st = float(stock)
            out = out_map.get(iid, 0.0)
            turns = round(out / st, 2) if st > 0 else 0.0
            tdays = round(days / turns, 1) if turns > 0 else None
            is_dead = st > 0 and out == 0
            dead += 1 if is_dead else 0
            items.append({"item_id": iid, "code": code, "name": name, "category": cat,
                          "out_qty": out, "current_stock": st, "turnover": turns,
                          "turnover_days": tdays, "dead_stock": is_dead})
        return {"since": str(since), "until": str(until), "days": days,
                "dead_stock_count": dead,
                "items": sorted(items, key=lambda x: (not x["dead_stock"], -x["current_stock"]))}

    async def cogs(self, session: AsyncSession, *, since: date, until: date) -> dict:
        """매출원가(출고원가, K-IFRS 1002) — 기간 출고(OUT/DISPOSE) 원가 합, 품목별·원가흐름 표시."""
        rows = (await session.execute(
            select(Item.id, Item.code, Item.name, Item.cost_method,
                   func.coalesce(func.sum(func.abs(StockMovement.qty)), 0),
                   func.coalesce(func.sum(StockMovement.cost), 0))
            .join(StockMovement, StockMovement.item_id == Item.id)
            .where(StockMovement.movement_type.in_(("OUT", "DISPOSE")),
                   StockMovement.moved_on >= since, StockMovement.moved_on <= until)
            .group_by(Item.id, Item.code, Item.name, Item.cost_method)
            .order_by(func.coalesce(func.sum(StockMovement.cost), 0).desc()))).all()
        items = [{"item_id": i, "code": c, "name": n, "cost_method": (m or "FIFO"),
                  "out_qty": float(q), "cogs": float(cost)} for i, c, n, m, q, cost in rows]
        return {"since": str(since), "until": str(until), "items": items,
                "total_cogs": round(sum(it["cogs"] for it in items))}

    # ── 저가법(NRV) 평가 (K-IFRS 1002) ───────────────────────────────
    async def nrv_preview(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """저가법 미리보기 — 품목별 장부가 vs 순실현가능가치(NRV), 평가손실·충당 차액.

        평가손실 = max(0, 장부가 − NRV평가액). NRV단가 미설정 품목은 제외(원가 유지).
        가중평균단가(wavg) 정보 제공. 충당 차액만 결산분개.
        """
        rows = (await session.execute(
            select(Item.id, Item.code, Item.name, Item.nrv_unit, Item.cost_method,
                   func.coalesce(func.sum(Lot.remaining_qty), 0),
                   func.coalesce(func.sum(Lot.remaining_qty * Lot.unit_cost), 0))
            # 원가 미상 lot은 평가 대상서 제외 — qty(전량)/book(원가있는것만) 불일치로
            # 평가손실이 과소산정되던 결함 방지(원가 없으면 저가법 비교 불가).
            .join(Lot, Lot.item_id == Item.id)
            .where(Lot.remaining_qty > 0, Lot.unit_cost.isnot(None))
            .group_by(Item.id, Item.code, Item.name, Item.nrv_unit, Item.cost_method))).all()
        items, required = [], 0.0
        for iid, code, name, nrv_unit, method, qty, book in rows:
            qty, book = float(qty), float(book)
            wavg = round(book / qty, 2) if qty else 0.0
            if nrv_unit is None:
                continue
            nrv_value = qty * float(nrv_unit)
            loss = max(0.0, book - nrv_value)
            if loss <= 0:
                continue
            required += loss
            items.append({"item_id": iid, "code": code, "name": name, "qty": qty,
                          "book_value": round(book), "wavg_unit": wavg,
                          "nrv_unit": float(nrv_unit), "nrv_value": round(nrv_value),
                          "loss": round(loss), "cost_method": method})
        current = await self._allowance_balance(session, as_of=_month_end(yyyymm))
        return {"yyyymm": yyyymm, "items": items, "required": round(required),
                "current": round(current), "delta": round(required) - round(current)}

    async def _allowance_balance(self, session: AsyncSession, *, as_of: date) -> float:
        from erp.finance.models import Voucher, VoucherLine
        d, cr = (await session.execute(
            select(
                func.coalesce(func.sum(case(
                    (VoucherLine.side == "DEBIT", VoucherLine.amount), else_=0)), 0),
                func.coalesce(func.sum(case(
                    (VoucherLine.side == "CREDIT", VoucherLine.amount), else_=0)), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code == ACC_INV_ALLOWANCE,
                   # 미승인·반려 수동전표 배제(statements 규약 — 계산검토 M-1)
                   Voucher.status.in_(("POSTED", "REVERSED")),
                   Voucher.voucher_on <= as_of))).one()
        return float(cr) - float(d)         # 평가충당금=대변잔액

    async def nrv_post(self, session: AsyncSession, *, yyyymm: str,
                       actor: int | None = None) -> dict:
        """저가법 평가손실 충당 차액 결산분개(멱등). 증가 5540/1159·환입 반대."""
        from erp.finance.service import VoucherLineDTO, VoucherService
        p = await self.nrv_preview(session, yyyymm=yyyymm)
        delta = p["delta"]
        if delta == 0:
            return {"posted": False, "reason": "조정 없음", **p}
        if delta > 0:
            lines = [VoucherLineDTO(ACC_INV_LOSS, "DEBIT", delta),
                     VoucherLineDTO(ACC_INV_ALLOWANCE, "CREDIT", delta)]
        else:
            lines = [VoucherLineDTO(ACC_INV_ALLOWANCE, "DEBIT", -delta),
                     VoucherLineDTO(ACC_INV_LOSS, "CREDIT", -delta)]
        v = await VoucherService().create(
            session, source_module="closing-ifrs", source_ref=f"inventory-nrv:{yyyymm}",
            voucher_on=_month_end(yyyymm), lines=lines,
            memo="재고자산 저가법 평가손실(K-IFRS 1002)", created_by=actor, allow_closed=True)
        return {"posted": True, "voucher_id": v.id, **p}
