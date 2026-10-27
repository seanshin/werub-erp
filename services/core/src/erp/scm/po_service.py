"""발주 워크플로우 (FR-14) — 요청→승인→입고. 구매 거버넌스 강화(구매 도메인 심화):
- 승인 시 직무분리(승인자≠요청자) + 예산 약정(encumber, 부서 지정 시) 연계.
- 입고 시 3-way 수량 검수(발주수량 대비 누적입고 검증·분할입고 PARTIAL) + 입고분만 미지급금.
- 완납(RECEIVED)·반려(REJECTED) 시 예산 약정 해제(가용예산 복원).
입고 시 StockService + 미지급금(재무) 연동.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.approval_engine import ApprovalEngine
from erp.errors import ValidationError
from erp.scm.models import PoLine, PurchaseOrder, Supplier
from erp.scm.stock_service import StockService

ACC_INVENTORY = "1150"   # 저장품(재고자산) — 입고 차변·기본 예산계정
_DOC = "PO"
_audit = AuditService()
_ENT = "scm.purchase_orders"


class PurchaseOrderService:
    def __init__(self, stock: StockService | None = None, settlement=None, budget=None):
        self._stock = stock or StockService()
        if settlement is None:
            from erp.finance.settlement_service import SettlementService
            settlement = SettlementService()
        self._settle = settlement
        if budget is None:
            from erp.finance.budget_service import BudgetService
            budget = BudgetService()
        self._budget = budget
        self._approval = ApprovalEngine()

    async def request(self, session: AsyncSession, *, supplier_id: int, actor: int,
                      lines: list[dict], department_id: int | None = None,
                      account_code: str = ACC_INVENTORY,
                      price_agreement_id: int | None = None) -> PurchaseOrder:
        if not lines:
            raise ValidationError("발주 라인이 없습니다")
        sup = await session.get(Supplier, supplier_id)   # 공급사 상태 게이트 — 정지/해지/미승인 벤더 발주 차단
        if sup is None:
            raise ValidationError("존재하지 않는 공급사입니다")
        if not sup.is_active:
            raise ValidationError("발주 가능한 공급사가 아닙니다(승인 상태만 발주 가능)",
                                  {"supplier_id": supplier_id, "status": sup.status})
        # 2026-10-09 전체 흐름 검토 E: 같은 품목이 두 라인이면 receive(품목키 dict)가 한 라인을
        # 덮어써 숨은 라인이 영영 미입고인데도 RECEIVED로 판정되고, 반품의 라인 조회도 깨진다.
        # 입고·반품이 품목 단위로 라인을 식별하므로 발주 단계에서 품목 중복을 거부(수량 합산은 요청자 몫).
        item_ids = [line["item_id"] for line in lines]
        if len(item_ids) != len(set(item_ids)):
            dup = sorted({i for i in item_ids if item_ids.count(i) > 1})
            raise ValidationError("같은 품목을 여러 라인으로 발주할 수 없습니다 (수량을 합산하세요)",
                                  {"item_ids": dup})
        prices = await self._agreement_prices(session, price_agreement_id, supplier_id)
        po = PurchaseOrder(supplier_id=supplier_id, requested_by=actor,
                           department_id=department_id, account_code=account_code)
        session.add(po)
        await session.flush()
        for line in lines:
            # 단가계약 지정 시 계약단가 자동 적용(미수록 품목 차단), 아니면 라인 단가.
            if prices is not None:
                if line["item_id"] not in prices:
                    raise ValidationError("단가계약에 없는 품목입니다",
                                          {"item_id": line["item_id"]})
                unit_price = prices[line["item_id"]]
            else:
                unit_price = line["unit_price"]
            session.add(PoLine(po_id=po.id, item_id=line["item_id"],
                               qty=line["qty"], unit_price=unit_price))
        await _audit.record_audit(session, actor=actor, action="CREATE", entity=_ENT,
                                  target_id=str(po.id),
                                  after={"supplier_id": supplier_id, "lines": len(lines),
                                         "department_id": department_id})
        # 2026-10-09 시나리오 S-D M2: 발주는 별도 상신 단계 없이 요청=상신(REQUESTED)이라 결재선을
        # 요청 시점에 만든다 — 종전엔 첫 approve 호출에서야 생겨 1단계가 결재함·알림에 안 떴다.
        await self._ensure_chain(session, po)
        return po

    async def _agreement_prices(self, session: AsyncSession, agreement_id: int | None,
                                supplier_id: int) -> dict | None:
        """단가계약 품목→계약단가 맵 — 미지정 시 None. 공급사 일치·ACTIVE 검증."""
        if agreement_id is None:
            return None
        from erp.scm.models import PriceAgreement, PriceAgreementLine
        pa = await session.get(PriceAgreement, agreement_id)
        if pa is None or pa.supplier_id != supplier_id:
            raise ValidationError("발주 공급사와 단가계약 공급사가 일치하지 않습니다")
        if pa.status != "ACTIVE":
            raise ValidationError(f"유효한 단가계약이 아닙니다 ({pa.status})")
        today = date.today()    # 유효기간 검증(active_price와 일관) — 만료 단가 적용 방지
        if not (pa.valid_from <= today <= pa.valid_to):
            raise ValidationError(f"단가계약 유효기간이 아닙니다 ({pa.valid_from}~{pa.valid_to})")
        rows = (await session.scalars(select(PriceAgreementLine).where(
            PriceAgreementLine.agreement_id == agreement_id))).all()
        return {r.item_id: float(r.unit_price) for r in rows}

    async def approve(self, session: AsyncSession, *, po_id: int, actor: int,
                      override: bool = False) -> PurchaseOrder:
        """승인 — 결재선 룰 있으면 다단계(현 단계 승인·완주 시 확정), 없으면 레거시 단일 승인.

        다단계 미완주(다음 단계 대기) 시 po.status는 REQUESTED 유지(승인 미확정).
        override=True(최고경영자 전권): DIRECTOR가 결재선 건너뛰어 즉시 확정(직무분리 유지).
        """
        po = await self._get(session, po_id)
        if po.status != "REQUESTED":
            raise ValidationError("승인 가능한 상태가 아닙니다", {"status": po.status})
        inst = await self._ensure_chain(session, po)
        if inst is None:                       # 룰 없음 → 레거시 단일 승인(직무분리)
            if po.requested_by == actor:
                raise ValidationError(
                    "직무분리 — 요청자 본인은 발주를 승인할 수 없습니다 (다른 담당자 승인 필요)")
            return await self._finalize_approval(session, po, actor)
        out = await self._approval.act(session, doc_type=_DOC, doc_id=po_id, actor=actor,
                                       approve=True, override=override)
        if out.status == "APPROVED":           # 전 단계 완주(또는 전권) → 발주 확정
            return await self._finalize_approval(session, po, actor)
        return po                              # 다음 단계 대기

    async def reject(self, session: AsyncSession, *, po_id: int, actor: int,
                     reason: str = "") -> PurchaseOrder:
        po = await self._get(session, po_id)
        if po.status not in ("REQUESTED", "APPROVED"):
            raise ValidationError("반려 가능한 상태가 아닙니다", {"status": po.status})
        # 진행 중 결재선이 있으면 단계 반려로 기록(직무분리·이력), 없으면 레거시 즉시 반려.
        inst = await self._approval.get(session, doc_type=_DOC, doc_id=po_id)
        if inst is not None and inst.get("status") == "PENDING":
            await self._approval.act(session, doc_type=_DOC, doc_id=po_id, actor=actor,
                                     approve=False, note=reason)
        po.status = "REJECTED"
        await self._release_encumbrance(session, po_id)   # 약정 해제(가용예산 복원)
        # 구매요청(PR) 전환분이면 PR을 APPROVED로 되돌림 — CONVERTED 데드엔드(부서 수요 무음
        # 소실) 방지(v1.203). po_id 해제로 재전환 가능.
        from erp.scm.models import PurchaseRequisition
        preq = await session.scalar(select(PurchaseRequisition).where(
            PurchaseRequisition.po_id == po_id,
            PurchaseRequisition.status == "CONVERTED").with_for_update())
        if preq is not None:
            preq.status, preq.po_id = "APPROVED", None
        await _audit.record_audit(session, actor=actor, action="REJECT", entity=_ENT,
                                  target_id=str(po_id), after={"status": "REJECTED",
                                                               "reason": reason,
                                                               "reopened_pr": preq.id
                                                               if preq else None})
        return po

    async def _ensure_chain(self, session: AsyncSession, po: PurchaseOrder):
        """진행 중 결재선 반환 — 없으면 룰로 생성 시도(룰 없으면 None=레거시)."""
        inst = await self._approval.get(session, doc_type=_DOC, doc_id=po.id)
        if inst is not None and inst.get("status") == "PENDING":
            return inst
        return await self._approval.build(
            session, doc_type=_DOC, doc_id=po.id,
            amount=await self._po_total(session, po.id),
            created_by=po.requested_by, department_id=po.department_id)

    async def _finalize_approval(self, session: AsyncSession, po: PurchaseOrder,
                                 actor: int) -> PurchaseOrder:
        po.status = "APPROVED"
        po.approved_by = actor
        # 예산 약정 — 부서 지정 시 발주총액 예약(가용예산 차감). source_ref로 멱등.
        # 회계연도는 발주 요청일 기준(연말 요청·연초 승인 시 약정연도 어긋남 방지).
        if po.department_id is not None:
            fy = (po.requested_at.year if po.requested_at else date.today().year)
            await self._budget.encumber(
                session, department_id=po.department_id,
                fiscal_year=fy, account_code=po.account_code,
                amount=await self._po_total(session, po.id),
                description=f"발주 #{po.id}", source_ref=f"po-{po.id}", user_id=actor)
        await _audit.record_audit(session, actor=actor, action="APPROVE", entity=_ENT,
                                  target_id=str(po.id), after={"status": "APPROVED"})
        return po

    async def receive(self, session: AsyncSession, *, po_id: int, actor: int,
                      receipts: list[dict], received_on: date) -> PurchaseOrder:
        """입고검수 — 3-way(발주수량 대비 누적입고 검증) + 입고분만 미지급금. 분할입고=PARTIAL."""
        # 행잠금 — 동시 입고 경합(누적수량 lost update·초과입고·receipt_count 충돌) 방지
        po = await session.scalar(select(PurchaseOrder).where(
            PurchaseOrder.id == po_id).with_for_update())
        if po is None:
            raise ValidationError("존재하지 않는 발주입니다")
        if po.status not in ("APPROVED", "PARTIAL"):
            raise ValidationError("승인된 발주만 입고할 수 있습니다", {"status": po.status})
        all_lines = (await session.execute(
            select(PoLine).where(PoLine.po_id == po_id).with_for_update())).scalars().all()
        lines = {pl.item_id: pl for pl in all_lines}
        if len(lines) != len(all_lines):
            # 2026-10-09 전체 흐름 검토 E: 중복 품목 라인(신규는 request가 거부·과거 데이터 방어)이면
            # dict가 라인을 잃어 완납 판정이 틀린다 — 조용히 RECEIVED 처리하지 않고 거부.
            raise ValidationError("같은 품목 라인이 중복된 발주라 입고를 판정할 수 없습니다 "
                                  "(발주 정정 필요)", {"po_id": po_id})
        amount = 0.0
        for r in receipts:
            pl = lines.get(r["item_id"])
            if pl is None:   # 미발주 품목 입고 차단 — 0원 lot·미지급 누락 방지
                raise ValidationError("발주에 없는 품목은 입고할 수 없습니다",
                                      {"item_id": r["item_id"], "po_id": po_id})
            if float(r["qty"]) <= 0:   # 음수/0 입고 차단 — 재고 역감·음수 미지급 방지
                raise ValidationError("입고 수량은 0보다 커야 합니다",
                                      {"item_id": r["item_id"], "qty": r["qty"]})
            new_received = float(pl.received_qty) + float(r["qty"])
            if new_received > float(pl.qty):   # 3-way — 발주수량 초과 입고 차단
                raise ValidationError(
                    "발주수량을 초과하는 입고입니다 (3-way 검수)",
                    {"item_id": r["item_id"], "ordered": float(pl.qty),
                     "already_received": float(pl.received_qty), "now": float(r["qty"])})
            await self._stock.receive(session, item_id=r["item_id"], lot_no=r["lot_no"],
                                      expiry_on=r["expiry_on"], qty=r["qty"],
                                      moved_on=received_on, actor=actor,
                                      supplier_id=po.supplier_id, unit_cost=float(pl.unit_price))
            pl.received_qty = new_received
            amount += float(r["qty"]) * float(pl.unit_price)
        # 완납 여부 — 전 라인 발주수량 충족 시 RECEIVED, 아니면 PARTIAL(분할입고)
        fully = all(float(pl.received_qty) >= float(pl.qty) for pl in all_lines)
        po.status = "RECEIVED" if fully else "PARTIAL"
        po.received_at = datetime.now(timezone.utc)
        po.receipt_count += 1
        # 거래처정산 — 이번 입고분만 미지급금(재고자산 차변/미지급 대변). 분할입고 멱등키=receipt_count.
        if round(amount) > 0:
            sup = await session.get(Supplier, po.supplier_id)
            await self._settle.record_payable(
                session, party=sup.name if sup else f"공급사{po.supplier_id}",
                party_ref=f"supplier:{po.supplier_id}",   # 거래처 마스터 안정 연계키(0216)
                amount=round(amount), incurred_on=received_on,
                due_on=received_on + timedelta(days=30),
                description=f"발주 #{po_id} 입고 #{po.receipt_count}", expense_account=ACC_INVENTORY,
                source_module="scm", source_ref=f"po-{po_id}-r{po.receipt_count}", created_by=actor)
        if fully:
            await self._release_encumbrance(session, po_id)   # 완납 → 약정 해제(집행 확정)
        await _audit.record_audit(session, actor=actor, action="RECEIVE", entity=_ENT,
                                  target_id=str(po_id),
                                  after={"status": po.status, "receipt": po.receipt_count,
                                         "amount": round(amount)})
        return po

    async def _po_total(self, session: AsyncSession, po_id: int) -> float:
        return round(sum(float(pl.qty) * float(pl.unit_price) for pl in (await session.execute(
            select(PoLine).where(PoLine.po_id == po_id))).scalars().all()))

    async def _release_encumbrance(self, session: AsyncSession, po_id: int) -> None:
        from erp.finance.models import Encumbrance
        enc = await session.scalar(select(Encumbrance).where(
            Encumbrance.source_ref == f"po-{po_id}", Encumbrance.status == "OPEN"))
        if enc is not None:
            await self._budget.release(session, encumbrance_id=enc.id)

    async def _get(self, session: AsyncSession, po_id: int) -> PurchaseOrder:
        po = (await session.execute(
            select(PurchaseOrder).where(PurchaseOrder.id == po_id))).scalar_one_or_none()
        if po is None:
            raise ValidationError("존재하지 않는 발주입니다")
        return po
