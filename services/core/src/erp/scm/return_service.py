"""입고 검수·반품(RMA·내부운영 갭 감사 Tier1·마이그 0214) — 공급사 반품 + credit note.

audit 발견: receive는 수량 3-way만·품질검수(합격/불합격/파손)·공급사 반품·credit note(미지급 차감) 전무.
본 서비스가 입고 후 불량/파손/유효기간/오배송 반품을 처리:
  로트 잔량 차감(RETURN 이동) + PoLine.received_qty 감소(교체 재입고 재개) + credit note(DR 2110/CR 1150)
  + PO 미지급 차감(AP aging 정합). 반품 수량은 로트 잔량 이내(소비/폐기분 반품 불가).
"""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.finance.models import Payable, Receivable
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.scm.models import Lot, PoLine, PurchaseOrder, PurchaseReturn, StockMovement, Supplier

_audit = AuditService()
ACC_INVENTORY = "1150"     # 저장품(재고자산)
ACC_AP = "2110"            # 미지급금
ACC_AR = "1130"            # 미수금(공급사 반품 환입)
ACC_INV_VARIANCE = "5540"  # 재고자산평가손익(F4 — 장부가 vs 청구단가 차액·valuation_service와 동일)
_REASONS = {"DEFECT", "DAMAGE", "EXPIRY", "WRONG_ITEM", "OTHER"}


class PurchaseReturnService:
    def __init__(self, vouchers: VoucherService | None = None):
        self._vs = vouchers or VoucherService()

    async def create_return(self, session: AsyncSession, *, po_id: int, lot_id: int,
                            qty: float, reason: str, returned_on: date, actor: int,
                            note: str | None = None) -> PurchaseReturn:
        reason = (reason or "").upper()
        if reason not in _REASONS:
            raise ValidationError("반품 사유 오류", {"reason": reason})
        if float(qty) <= 0:
            raise ValidationError("반품 수량은 0보다 커야 합니다")
        po = await session.scalar(select(PurchaseOrder).where(
            PurchaseOrder.id == po_id).with_for_update())
        if po is None:
            raise ValidationError("존재하지 않는 발주입니다")
        lot = await session.scalar(select(Lot).where(Lot.id == lot_id).with_for_update())
        if lot is None:
            raise ValidationError("존재하지 않는 로트입니다")
        pl = await session.scalar(select(PoLine).where(
            PoLine.po_id == po_id, PoLine.item_id == lot.item_id).with_for_update())
        if pl is None:
            raise ValidationError("해당 발주의 품목이 아닙니다",
                                  {"po_id": po_id, "item_id": lot.item_id})
        qty = float(qty)
        # 2026-10-09 전체 흐름 검토 E: 로트가 이 발주의 공급사에서 온 것인지, 반품 수량이 이 발주
        # 라인의 입고수량 이내인지 검증하지 않아, 같은 품목의 타 공급사 로트(또는 미입고 발주)로
        # 반품을 걸면 미지급 감액·환입 미수금(가짜 채권)·평가이익이 생겼다.
        if lot.supplier_id is not None and lot.supplier_id != po.supplier_id:
            raise ValidationError("다른 공급사에서 입고된 로트는 이 발주로 반품할 수 없습니다",
                                  {"po_id": po_id, "lot_id": lot_id})
        if lot.supplier_id is None:
            # 공급사 미상 로트(초기 이관·조정 입고)는 발주 반품 근거가 없다
            raise ValidationError("입고 공급사가 없는 로트는 발주 반품할 수 없습니다",
                                  {"lot_id": lot_id})
        if qty > float(lot.remaining_qty):    # 소비/폐기분 반품 불가(잔량 이내)
            raise ValidationError("반품 수량이 로트 잔량을 초과합니다",
                                  {"remaining": float(lot.remaining_qty), "qty": qty})
        if qty > float(pl.received_qty):
            raise ValidationError("반품 수량이 발주 라인 입고수량을 초과합니다",
                                  {"received": float(pl.received_qty), "qty": qty})
        # F4 — 재고 반제는 장부단가(lot.unit_cost·WAVG 이동평균 재기입 반영)로, 공급사 환입은
        # 청구단가(PoLine.unit_price)로 분리. 입고 후 WAVG 출고가 lot.unit_cost를 이동평균으로
        # 재기입하면 장부가 ≠ PO단가가 되는데, 이때 AP를 장부가로 감액하면 공급사가 실제 환입하는
        # 청구액과 괴리(부채 과대/과소). 차액은 재고평가손익(5540)으로 흡수.
        carry_unit = float(lot.unit_cost if lot.unit_cost is not None else pl.unit_price)
        carry_amount = round(qty * carry_unit)    # 재고 장부 반제액(CR 1150)
        po_amount = round(qty * float(pl.unit_price))   # 공급사 환입액(AP 감액/환입 미수)
        unit, amount = carry_unit, carry_amount   # PurchaseReturn/이동 = 재고 장부가치
        # 2026-10-09 시나리오 S-D M3: 감액될 미지급이 결의 중(DRAFT/APPROVED) 지급 배치에 있으면 반품을
        # 막는다 — 종전엔 미지급만 줄고 배치 라인·합계는 옛 금액이라 이체파일이 과지급하고 실행은
        # "지급액이 미지급 잔액을 초과"로 실패해 배치가 멈췄다(개별 지급 차단과 같은 규칙).
        await self._assert_cut_payables_not_in_batch(session, po_id=po_id, credit=po_amount)
        # 로트 잔량 차감 + RETURN 이동
        lot.remaining_qty = float(lot.remaining_qty) - qty
        session.add(StockMovement(item_id=lot.item_id, lot_id=lot.id, movement_type="RETURN",
                                  qty=qty, moved_on=returned_on, actor=actor,
                                  cost=amount, override_reason=f"반품:{reason}"))
        # 입고수량 되돌림(교체 재입고 재개) + PO 상태
        pl.received_qty = max(0.0, float(pl.received_qty) - qty)
        all_lines = (await session.scalars(select(PoLine).where(PoLine.po_id == po_id))).all()
        fully = all(float(x.received_qty) >= float(x.qty) for x in all_lines)
        any_recv = any(float(x.received_qty) > 0 for x in all_lines)
        if po.status in ("RECEIVED", "PARTIAL"):
            po.status = "RECEIVED" if fully else ("PARTIAL" if any_recv else "APPROVED")
        ret = PurchaseReturn(po_id=po_id, supplier_id=po.supplier_id, item_id=lot.item_id,
                             lot_id=lot.id, qty=qty, unit_cost=unit, amount=amount,
                             reason=reason, note=note, returned_on=returned_on,
                             created_by=actor)
        session.add(ret)
        await session.flush()
        # credit note — 재고 감소(CR 1150)는 장부가(carry_amount). 공급사 환입(po_amount)은
        # 미결제 미지급 잔액 한도(DR 2110)+초과분(이미 지급) 공급사 환입 미수금(DR 1130·감사 F3).
        # 장부가≠청구액 차액(F4)은 재고평가손익(5540)으로 흡수 — 장부가>청구액=평가손실(DR),
        # 장부가<청구액=평가차 환입(CR). 균형: 재고평가 반영으로 차대 일치.
        reducible = min(po_amount, await self._po_open_payable(session, po_id=po_id))
        refund = po_amount - reducible
        variance = po_amount - carry_amount    # >0: 청구>장부(CR 5540) · <0: 장부>청구(DR 5540)
        lines = [VoucherLineDTO(ACC_INVENTORY, "CREDIT", carry_amount)]
        if reducible > 0:
            lines.append(VoucherLineDTO(ACC_AP, "DEBIT", reducible))
        if refund > 0:
            lines.append(VoucherLineDTO(ACC_AR, "DEBIT", refund))   # 공급사 환입 미수금
        if variance > 0:
            lines.append(VoucherLineDTO(ACC_INV_VARIANCE, "CREDIT", variance))
        elif variance < 0:
            lines.append(VoucherLineDTO(ACC_INV_VARIANCE, "DEBIT", -variance))
        await self._vs.create(
            session, source_module="scm", source_ref=f"po-return-{ret.id}",
            voucher_on=returned_on, created_by=actor, lines=lines,
            memo=f"입고 반품 발주#{po_id} {reason} (로트 {lot.lot_no})")
        ret.credit_voucher_ref = f"po-return-{ret.id}"
        # AP 미지급 차감 — reducible만큼 subledger 감액(GL과 정합). 환입분은 미수금 레코드.
        await self._reduce_po_payable(session, po_id=po_id, credit=reducible)
        if refund > 0:
            sup = await session.get(Supplier, po.supplier_id)
            session.add(Receivable(
                party=sup.name if sup else f"공급사{po.supplier_id}",
                description=f"발주 #{po_id} 반품 환입", amount=round(refund), collected_amount=0,
                billed_on=returned_on, due_on=returned_on + timedelta(days=30),
                status="OUTSTANDING", source_module="scm",
                source_ref=f"po-return-refund-{ret.id}"))
        await _audit.record_audit(session, actor=actor, action="RETURN", entity="scm.purchase_orders",
                                  target_id=str(po_id),
                                  after={"return_id": ret.id, "qty": qty, "amount": amount,
                                         "reason": reason})
        return ret

    async def _po_open_payable(self, session: AsyncSession, *, po_id: int) -> float:
        """PO 미결제 미지급 잔액 합(감사 F3 — 반품 부채감소 한도 산정)."""
        rows = (await session.scalars(
            select(Payable).where(
                Payable.source_module == "scm",
                Payable.source_ref.like(f"po-{po_id}-r%"),
                Payable.status != "PAID"))).all()
        return sum(max(0.0, float(p.amount) - float(p.paid_amount)) for p in rows)

    async def _assert_cut_payables_not_in_batch(self, session: AsyncSession, *, po_id: int,
                                                credit: float) -> None:
        """_reduce_po_payable이 감액할 미지급(오래된 것부터)이 열린 지급 배치에 있으면 거부."""
        if credit <= 0:
            return
        from erp.finance.disbursement_service import DisbursementService
        disb = DisbursementService()
        rows = (await session.scalars(
            select(Payable).where(
                Payable.source_module == "scm",
                Payable.source_ref.like(f"po-{po_id}-r%"),
                Payable.status != "PAID").order_by(Payable.id))).all()
        remaining = credit
        for p in rows:
            if remaining <= 0:
                break
            outstanding = float(p.amount) - float(p.paid_amount)
            if outstanding <= 0:
                continue
            try:
                await disb.assert_not_in_open_batch(session, payable_id=p.id)
            except ValidationError as exc:
                raise ValidationError(
                    "반품으로 감액할 미지급금이 진행 중인 지급결의에 포함돼 있습니다 — 자금 담당자가 그 결의를 "
                    "취소(또는 실행)한 뒤 반품을 등록하세요",
                    {"po_id": po_id, "payable_id": p.id, **(exc.details or {})}) from exc
            remaining -= min(outstanding, remaining)

    async def _reduce_po_payable(self, session: AsyncSession, *, po_id: int,
                                 credit: float) -> None:
        """PO 미결제 미지급을 credit만큼 감액(오래된 것부터·AP aging 정합)."""
        if credit <= 0:
            return
        rows = (await session.scalars(
            select(Payable).where(
                Payable.source_module == "scm",
                Payable.source_ref.like(f"po-{po_id}-r%"),
                Payable.status != "PAID").order_by(Payable.id).with_for_update())).all()
        remaining = credit
        for p in rows:
            if remaining <= 0:
                break
            outstanding = float(p.amount) - float(p.paid_amount)
            if outstanding <= 0:
                continue
            cut = min(outstanding, remaining)
            p.amount = float(p.amount) - cut
            if float(p.amount) <= float(p.paid_amount):
                p.status = "PAID"
            remaining -= cut

    async def list_returns(self, session: AsyncSession, *, po_id: int | None = None) -> list[dict]:
        q = select(PurchaseReturn)
        if po_id is not None:
            q = q.where(PurchaseReturn.po_id == po_id)
        rows = (await session.scalars(q.order_by(PurchaseReturn.id.desc()).limit(500))).all()
        return [{"id": r.id, "po_id": r.po_id, "item_id": r.item_id, "lot_id": r.lot_id,
                 "qty": float(r.qty), "amount": float(r.amount), "reason": r.reason,
                 "note": r.note, "returned_on": r.returned_on,
                 "credit_voucher_ref": r.credit_voucher_ref} for r in rows]
