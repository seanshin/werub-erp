"""FIFO 재고 엔진 (FR-12/46/13) — design §3.5 동시성 규칙.

- lot 선택: SELECT ... FOR UPDATE, 유효기간·입고일 순 — SKIP LOCKED 금지 (FIFO 순서 보존)
- 유효기간 3개월 미만 lot 출고 차단 (관리자 override 시 사유 기록)
- 안전재고 미달 시 In-app 알림 (FR-13)
"""
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.models import Notification, User
from erp.errors import ERPException, ValidationError
from erp.scm.models import Item, Lot, StockMovement

EXPIRY_BLOCK_DAYS = 90  # 3개월 (PRD 2.1)
ACC_INVENTORY = "1150"  # 저장품(재고자산)
ACC_COGS = "5410"       # 재료비(매출원가) — 출고 소비분 대체


class ExpiryBlockedError(ERPException):
    def __init__(self, lot_no: str, expiry_on: date):
        super().__init__("ERP-3002", "유효기간 임박 lot — 출고가 차단되었습니다 (관리자 override 필요)",
                         {"lot_no": lot_no, "expiry_on": expiry_on.isoformat()}, status_code=409)


class InsufficientStockError(ERPException):
    def __init__(self, item_id: int, requested: float, available: float):
        super().__init__("ERP-3001", "재고가 부족합니다",
                         {"item_id": item_id, "requested": requested,
                          "available": available}, status_code=409)


class StockService:
    async def receive(self, session: AsyncSession, *, item_id: int, lot_no: str,
                      expiry_on: date, qty: float, moved_on: date, actor: int,
                      supplier_id: int | None = None, unit_cost: float | None = None) -> Lot:
        """입고 — 로트·유효기간 필수 (FR-11). unit_cost=매입단가(재고평가)."""
        if qty <= 0:
            raise ValidationError("입고 수량은 0보다 커야 합니다")
        lot = Lot(item_id=item_id, lot_no=lot_no, expiry_on=expiry_on,
                  received_on=moved_on, received_qty=qty, remaining_qty=qty,
                  unit_cost=unit_cost, supplier_id=supplier_id)
        session.add(lot)
        await session.flush()
        session.add(StockMovement(item_id=item_id, lot_id=lot.id, movement_type="IN",
                                  qty=qty, moved_on=moved_on, actor=actor))
        return lot

    async def issue(self, session: AsyncSession, *, item_id: int, qty: float,
                    moved_on: date, actor: int,
                    override_expiry: bool = False,
                    override_reason: str | None = None) -> list[StockMovement]:
        """FIFO 출고 — 유효기간 임박·만료 lot 차단, FOR UPDATE 직렬화."""
        if qty <= 0:
            raise ValidationError("출고 수량은 0보다 커야 합니다")
        if override_expiry and not override_reason:
            raise ValidationError("override 사유는 필수입니다")

        block_before = moved_on + timedelta(days=EXPIRY_BLOCK_DAYS)
        # FIFO: 유효기간 → 입고일 순. FOR UPDATE (SKIP LOCKED 금지 — 순서 보존, design v0.3)
        lots = (await session.execute(
            select(Lot)
            .where(Lot.item_id == item_id, Lot.remaining_qty > 0)
            .order_by(Lot.expiry_on, Lot.received_on, Lot.id)
            .with_for_update()
        )).scalars().all()

        available = sum(float(lot.remaining_qty) for lot in lots
                        if override_expiry or lot.expiry_on >= block_before)
        if available < qty:
            raise InsufficientStockError(item_id, qty, available)

        # 원가흐름(K-IFRS 1002) — 물리 출고는 FIFO(유효기간) 유지, 원가 산정은 cost_method.
        # WAVG: 출고 전 이동평균단가(잔량가중) 적용. FIFO: 소비 lot의 실제 매입단가.
        item = await session.get(Item, item_id)
        method = (item.cost_method if item else "FIFO") or "FIFO"
        tot_qty = sum(float(lot.remaining_qty) for lot in lots)
        tot_val = sum(float(lot.remaining_qty) * float(lot.unit_cost or 0) for lot in lots)
        wavg = (tot_val / tot_qty) if tot_qty else 0.0

        movements: list[StockMovement] = []
        remaining = qty
        for lot in lots:
            if remaining <= 0:
                break
            if lot.expiry_on < block_before and not override_expiry:
                continue  # 임박·만료 lot — 차단 (가용량 계산과 동일 기준, 분석 L2)
            take = min(float(lot.remaining_qty), remaining)
            lot.remaining_qty = float(lot.remaining_qty) - take
            unit = wavg if method == "WAVG" else float(lot.unit_cost or 0)
            mv = StockMovement(item_id=item_id, lot_id=lot.id, movement_type="OUT",
                               qty=take, moved_on=moved_on, actor=actor,
                               override_reason=override_reason if override_expiry else None,
                               cost=round(take * unit, 2))
            session.add(mv)
            movements.append(mv)
            remaining -= take
        # WAVG(이동평균) 정합 — 출고분을 wavg로 relief했으면 잔여 재고도 wavg로 이월해야
        # GL(1150)과 재고평가(Σ remaining×unit_cost)가 일치. 미이월 시 relief=wavg·평가=원가lot로
        # 영구 괴리(원가 다른 lot이 FIFO로 소진될수록 누적). FIFO 품목은 실 lot원가 유지(무영향).
        if method == "WAVG" and tot_qty:
            for lot in lots:
                if float(lot.remaining_qty) > 0:
                    lot.unit_cost = round(wavg, 2)
        await session.flush()
        # 매출원가 자동전기(K-IFRS 1002) — 출고 소비분 재료비(5410)/재고자산(1150) 대체.
        # 실제 차감 로트원가 합(cost_method 반영) 기준. 원가 0(미상 lot뿐)이면 미전기.
        total_cost = round(sum(float(m.cost or 0) for m in movements))
        if total_cost > 0:
            from erp.finance.service import VoucherLineDTO, VoucherService
            await VoucherService().create(
                session, source_module="scm.issue",
                source_ref=f"stock-issue:{movements[0].id}", voucher_on=moved_on,
                lines=[VoucherLineDTO(ACC_COGS, "DEBIT", total_cost),
                       VoucherLineDTO(ACC_INVENTORY, "CREDIT", total_cost)],
                created_by=actor, memo=f"출고 매출원가 (품목 {item_id})")
        await self._check_safety_stock(session, item_id)
        return movements

    async def current_stock(self, session: AsyncSession, *, item_id: int) -> float:
        return float((await session.execute(
            select(func.coalesce(func.sum(Lot.remaining_qty), 0))
            .where(Lot.item_id == item_id))).scalar_one())

    async def expiring_lots(self, session: AsyncSession, *, within_days: int = 90,
                            today: date | None = None) -> list[Lot]:
        """유효기간 임박 lot (FR-46 알림 입력)."""
        base = today or date.today()
        return list((await session.execute(
            select(Lot).where(Lot.remaining_qty > 0,
                              Lot.expiry_on <= base + timedelta(days=within_days),
                              Lot.expiry_on >= base)
            .order_by(Lot.expiry_on))).scalars().all())

    async def _check_safety_stock(self, session: AsyncSession, item_id: int) -> None:
        """안전재고 미달 알림 (FR-13) — 담당(관리자·부서장)에게 In-app."""
        item = (await session.execute(
            select(Item).where(Item.id == item_id))).scalar_one()
        if float(item.safety_stock_qty) <= 0:
            return
        stock = await self.current_stock(session, item_id=item_id)
        if stock < float(item.safety_stock_qty):
            targets = (await session.execute(
                select(User.id).where(User.role.in_(["ADMIN", "MANAGER"])))).scalars().all()
            for uid in targets:
                session.add(Notification(
                    user_id=uid, type="STOCK",
                    title=f"안전재고 미달: {item.name}",
                    body=f"현재 {stock} < 안전재고 {float(item.safety_stock_qty)} ({item.code})"))
