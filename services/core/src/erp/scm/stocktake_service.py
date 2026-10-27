"""재고 실사 — 실재고 vs 장부재고 차이를 로트 조정·전표화 (FR-46 확장).

record: 장부재고(잔량 합)·가중평균단가 산정 → 차이. apply: 부족분 FIFO 차감(감모손실 5530/재고
1150)·과잉분 로트 추가(재고 1150/조정이익 4620). 멱등(DRAFT만 적용).
"""
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.scm.models import Item, Lot, StockMovement, StockTake

ACC_INVENTORY = "1150"   # 저장품(재고자산)
ACC_SHRINK = "5530"      # 재고감모손실
ACC_GAIN = "4620"        # 재고조정이익
_FAR = date(2099, 12, 31)


class StockTakeService:
    async def _avg_cost(self, session: AsyncSession, item_id: int) -> float:
        lots = (await session.execute(select(Lot).where(
            Lot.item_id == item_id, Lot.remaining_qty > 0))).scalars().all()
        qty = sum(float(lot.remaining_qty) for lot in lots)
        val = sum(float(lot.remaining_qty) * float(lot.unit_cost or 0) for lot in lots)
        return round(val / qty, 2) if qty else 0.0

    async def record(self, session: AsyncSession, *, item_id: int, physical_qty: float,
                     taken_on: date, note: str = "", user_id: int | None = None) -> StockTake:
        """실사 기록 — 장부재고·차이 산정(DRAFT)."""
        if physical_qty < 0:
            raise ValidationError("실재고는 0 이상이어야 합니다")
        book = float(await session.scalar(
            select(func.coalesce(func.sum(Lot.remaining_qty), 0))
            .where(Lot.item_id == item_id)) or 0)
        cost = await self._avg_cost(session, item_id)
        diff = physical_qty - book
        st = StockTake(item_id=item_id, taken_on=taken_on, book_qty=book,
                       physical_qty=physical_qty, diff_qty=diff, unit_cost=cost,
                       diff_value=round(diff * cost), note=note or None, created_by=user_id)
        session.add(st)
        await session.flush()
        return st

    async def apply(self, session: AsyncSession, *, stocktake_id: int,
                    user_id: int | None = None) -> dict:
        """실사 반영 — 로트 조정 + 감모손실/조정이익 전표. DRAFT만."""
        from erp.finance.service import VoucherLineDTO, VoucherService
        # 행 잠금 — 멱등 가드(status 재확인)와 로트 차감을 원자적으로(동시/재시도 시 이중 반영 방지).
        st = await session.get(StockTake, stocktake_id, with_for_update=True)
        if st is None:
            raise ValidationError("존재하지 않는 실사입니다")
        if st.status == "APPLIED":
            return {"id": st.id, "status": "APPLIED", "voucher_ref": st.voucher_ref}
        diff = float(st.diff_qty)
        ref = f"stocktake:{st.id}"
        voucher_id = None
        if round(diff, 2) < 0:                       # 부족 — FIFO 차감 + 감모손실
            need = -diff
            # 로트 행잠금 — issue()와 동일 패턴. StockTake 행잠금(멱등)만으로는 로트 수량을
            # 보호하지 못해, 동시 출고/실사가 remaining_qty를 lost-update → 영구재고↔GL(1150)
            # 괴리(전체검토 stocktake TOCTOU). FOR UPDATE로 apply를 issue·타 apply와 직렬화하고
            # 잠금 후 재판독한 잔량에서 차감해 GL 감소액과 영구재고를 정합.
            lots = (await session.execute(select(Lot).where(
                Lot.item_id == st.item_id, Lot.remaining_qty > 0)
                .order_by(Lot.expiry_on, Lot.id).with_for_update())).scalars().all()
            deducted = deducted_value = 0.0
            for lot in lots:
                if need <= 0:
                    break
                take = min(float(lot.remaining_qty), need)
                lot.remaining_qty = float(lot.remaining_qty) - take
                session.add(StockMovement(item_id=st.item_id, lot_id=lot.id,
                                          movement_type="ADJUST", qty=-take,
                                          moved_on=st.taken_on, actor=user_id or 0))
                need -= take
                deducted += take
                deducted_value += take * float(lot.unit_cost or 0)   # 실제 차감 로트원가
            # 실제 차감 로트원가 합 기준 전표 — FIFO 로트별 단가차가 있어도 GL이 실제
            # 재고가치 감소액과 정확히 일치(가중평균 근사로 인한 GL·영구재고 괴리 방지).
            val = round(deducted_value)
            if val:
                v = await VoucherService().create(
                    session, source_module="scm.stocktake", source_ref=ref, voucher_on=st.taken_on,
                    lines=[VoucherLineDTO(ACC_SHRINK, "DEBIT", val),
                           VoucherLineDTO(ACC_INVENTORY, "CREDIT", val)],
                    created_by=user_id, memo=f"재고 실사 감모 (품목 {st.item_id})")
                voucher_id = v.id
        elif round(diff, 2) > 0:                     # 과잉 — 로트 추가 + 조정이익
            lot = Lot(item_id=st.item_id, lot_no=f"STK-{st.id}", expiry_on=_FAR,
                      received_on=st.taken_on, received_qty=diff, remaining_qty=diff,
                      unit_cost=float(st.unit_cost))
            session.add(lot)
            await session.flush()
            session.add(StockMovement(item_id=st.item_id, lot_id=lot.id, movement_type="ADJUST",
                                      qty=diff, moved_on=st.taken_on, actor=user_id or 0))
            val = abs(float(st.diff_value))
            if val:
                v = await VoucherService().create(
                    session, source_module="scm.stocktake", source_ref=ref, voucher_on=st.taken_on,
                    lines=[VoucherLineDTO(ACC_INVENTORY, "DEBIT", val),
                           VoucherLineDTO(ACC_GAIN, "CREDIT", val)],
                    created_by=user_id, memo=f"재고 실사 과잉 (품목 {st.item_id})")
                voucher_id = v.id
        st.status = "APPLIED"
        st.voucher_ref = ref if voucher_id else None
        st.applied_at = datetime.now(timezone.utc)
        return {"id": st.id, "status": "APPLIED", "diff_qty": diff,
                "diff_value": float(st.diff_value), "voucher_id": voucher_id}

    async def list_takes(self, session: AsyncSession, *, status: str | None = None) -> list[dict]:
        q = select(StockTake, Item.code, Item.name).join(Item, Item.id == StockTake.item_id)
        if status:
            q = q.where(StockTake.status == status)
        rows = (await session.execute(q.order_by(StockTake.id.desc()).limit(100))).all()
        return [{"id": s.id, "item_id": s.item_id, "code": c, "name": n,
                 "taken_on": str(s.taken_on), "book_qty": float(s.book_qty),
                 "physical_qty": float(s.physical_qty), "diff_qty": float(s.diff_qty),
                 "diff_value": float(s.diff_value), "status": s.status} for s, c, n in rows]
