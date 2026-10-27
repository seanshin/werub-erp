"""위탁재고(Consignment) — 소유권 미이전 보관·사용 시점 매입 (내부운영 갭 감사 Tier2·마이그 0219).

고가 임플란트·스텐트·인공관절을 공급사 소유 상태로 병원 보관하다 사용 시점에 매입(소유권 이전).
- 적재(place): 회계 없음(미소유). 보관만 기록.
- 사용(consume): 소유권 이전 → DR 재료비(5410)/CR 미지급금(2110)·미지급(record_payable·party_ref
  거래처 연계·AP aging 정합). chart_no로 시술 환자 참조(환자 청구는 별도 흐름).
- 반품(return): 미사용분 공급사 반환 — 회계 없음(미소유).
일반 Lot(소유재고)과 분리 — 위탁재고는 재고자산(1150)·재고평가에 미포함.
"""
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.crypto import mask_chart_no
from erp.errors import ValidationError
from erp.finance.settlement_service import SettlementService
from erp.scm.models import ConsignmentConsumption, ConsignmentStock, Item, Supplier

_audit = AuditService()
ACC_MATERIAL = "5410"   # 재료비


class ConsignmentService:
    def __init__(self, settlement: SettlementService | None = None):
        self._settle = settlement or SettlementService()

    async def place(self, session: AsyncSession, *, supplier_id: int, item_id: int, lot_no: str,
                    qty: float, unit_cost: float, placed_on: date,
                    expiry_on: date | None = None, note: str | None = None) -> ConsignmentStock:
        """위탁재고 적재 — 공급사가 병원에 비치(미소유·회계 없음)."""
        if float(qty) <= 0:
            raise ValidationError("적재 수량은 0보다 커야 합니다")
        if float(unit_cost) <= 0:
            raise ValidationError("매입단가는 0보다 커야 합니다")
        if await session.get(Supplier, supplier_id) is None:
            raise ValidationError("존재하지 않는 공급사입니다", {"supplier_id": supplier_id})
        if await session.get(Item, item_id) is None:
            raise ValidationError("존재하지 않는 품목입니다", {"item_id": item_id})
        cs = ConsignmentStock(supplier_id=supplier_id, item_id=item_id, lot_no=lot_no,
                              expiry_on=expiry_on, placed_qty=qty, qty_on_hand=qty,
                              unit_cost=unit_cost, placed_on=placed_on, note=note, status="ACTIVE")
        session.add(cs)
        await session.flush()
        return cs

    async def _get(self, session: AsyncSession, consignment_id: int) -> ConsignmentStock:
        cs = await session.get(ConsignmentStock, consignment_id, with_for_update=True)
        if cs is None:
            raise ValidationError("존재하지 않는 위탁재고입니다", {"consignment_id": consignment_id})
        return cs

    async def consume(self, session: AsyncSession, *, consignment_id: int, qty: float,
                      consumed_on: date, chart_no: str | None = None, note: str | None = None,
                      actor: int | None = None,
                      client_ref: str | None = None) -> ConsignmentConsumption:
        """위탁재고 사용 — 소유권 이전·매입 인식(DR 5410/CR 2110)·미지급 발생. 잔량 이내.

        멱등(F5): client_ref 제공 시 동일 키 기존 사용이 있으면 재전송으로 보고 기존 레코드 반환
        (중복 매입인식·미지급·잔량 이중차감 방지). 부분 유니크 인덱스가 동시 중복 백스톱."""
        if client_ref:
            dup = await session.scalar(select(ConsignmentConsumption).where(
                ConsignmentConsumption.client_ref == client_ref))
            if dup is not None:
                return dup
        cs = await self._get(session, consignment_id)
        if cs.status != "ACTIVE":
            raise ValidationError("사용 가능한 위탁재고가 아닙니다", {"status": cs.status})
        qty = float(qty)
        if qty <= 0:
            raise ValidationError("사용 수량은 0보다 커야 합니다")
        if qty > float(cs.qty_on_hand):
            raise ValidationError("사용 수량이 위탁 잔량을 초과합니다",
                                  {"on_hand": float(cs.qty_on_hand), "qty": qty})
        amount = round(qty * float(cs.unit_cost))
        cs.qty_on_hand = float(cs.qty_on_hand) - qty
        if float(cs.qty_on_hand) == 0:
            cs.status = "DEPLETED"
        con = ConsignmentConsumption(consignment_id=consignment_id, qty=qty,
                                     unit_cost=float(cs.unit_cost), amount=amount,
                                     consumed_on=consumed_on, chart_no=chart_no, note=note,
                                     actor=actor, client_ref=client_ref)
        session.add(con)
        await session.flush()
        # 매입 인식 — DR 재료비(5410)/CR 미지급금(2110). record_payable이 Payable+전표 생성.
        sup = await session.get(Supplier, cs.supplier_id)
        await self._settle.record_payable(
            session, party=sup.name if sup else f"공급사{cs.supplier_id}",
            party_ref=f"supplier:{cs.supplier_id}", amount=amount, incurred_on=consumed_on,
            due_on=consumed_on + timedelta(days=30), expense_account=ACC_MATERIAL,
            description=f"위탁재고 사용 {cs.lot_no}", source_module="consignment",
            source_ref=f"consign-consume-{con.id}", created_by=actor)
        con.payable_ref = f"consign-consume-{con.id}"
        await _audit.record_audit(session, actor=actor or 0, action="CONSUME",
                                  entity="scm.consignment_stock", target_id=str(consignment_id),
                                  after={"consumption_id": con.id, "qty": qty, "amount": amount})
        return con

    async def return_stock(self, session: AsyncSession, *, consignment_id: int, qty: float,
                           returned_on: date, note: str | None = None,
                           actor: int | None = None) -> ConsignmentStock:
        """미사용 위탁재고 공급사 반환 — 회계 없음(미소유). 잔량 이내."""
        cs = await self._get(session, consignment_id)
        if cs.status != "ACTIVE":
            raise ValidationError("반품 가능한 위탁재고가 아닙니다", {"status": cs.status})
        qty = float(qty)
        if qty <= 0 or qty > float(cs.qty_on_hand):
            raise ValidationError("반품 수량이 위탁 잔량을 초과합니다",
                                  {"on_hand": float(cs.qty_on_hand), "qty": qty})
        cs.qty_on_hand = float(cs.qty_on_hand) - qty
        if float(cs.qty_on_hand) == 0:
            cs.status = "RETURNED"
        await _audit.record_audit(session, actor=actor or 0, action="RETURN",
                                  entity="scm.consignment_stock", target_id=str(consignment_id),
                                  after={"qty": qty, "on_hand": float(cs.qty_on_hand)})
        return cs

    async def list_stock(self, session: AsyncSession, *, status: str | None = "ACTIVE",
                         supplier_id: int | None = None) -> list[dict]:
        q = select(ConsignmentStock)
        if status:
            q = q.where(ConsignmentStock.status == status)
        if supplier_id is not None:
            q = q.where(ConsignmentStock.supplier_id == supplier_id)
        rows = (await session.scalars(q.order_by(ConsignmentStock.id.desc()).limit(500))).all()
        return [{"id": c.id, "supplier_id": c.supplier_id, "item_id": c.item_id,
                 "lot_no": c.lot_no, "expiry_on": c.expiry_on, "placed_qty": float(c.placed_qty),
                 "qty_on_hand": float(c.qty_on_hand), "unit_cost": float(c.unit_cost),
                 "status": c.status} for c in rows]

    async def list_consumptions(self, session: AsyncSession, *, since: date | None = None,
                                until: date | None = None, unmask_chart: bool = False) -> dict:
        """위탁재고 사용(매입) 내역.

        chart_no는 **기본 마스킹**한다 — 이 목록의 소비자는 조달·물류(scm.po read)이고
        환자 청구 대사는 별도 흐름이라 환자 특정이 필요 없다. 환자 식별 권한이 있는 호출자만
        `unmask_chart=True`로 원문을 받는다(라우터가 판정)."""
        q = select(ConsignmentConsumption)
        if since:
            q = q.where(ConsignmentConsumption.consumed_on >= since)
        if until:
            q = q.where(ConsignmentConsumption.consumed_on <= until)
        rows = (await session.scalars(q.order_by(ConsignmentConsumption.id.desc()).limit(500))).all()
        total = float(await session.scalar(
            select(func.coalesce(func.sum(ConsignmentConsumption.amount), 0))
            .where(*( [ConsignmentConsumption.consumed_on >= since] if since else [] )
                   + ( [ConsignmentConsumption.consumed_on <= until] if until else [] ))) or 0)
        return {"items": [{"id": c.id, "consignment_id": c.consignment_id, "qty": float(c.qty),
                           "amount": float(c.amount), "consumed_on": c.consumed_on,
                           "chart_no": (c.chart_no if unmask_chart else mask_chart_no(c.chart_no)),
                           "chart_masked": not unmask_chart,
                           "payable_ref": c.payable_ref} for c in rows],
                "total_amount": total}
