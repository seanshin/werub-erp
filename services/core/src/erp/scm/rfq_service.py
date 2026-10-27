"""소싱(RFQ→견적→단가계약) 서비스 — 구매 상류. PO는 단가계약 단가를 자동 적용.

흐름: create_rfq(품목·수량) → open → 벤더별 submit_quote(단가) → compare(총액 순위) →
award(낙찰 견적 → 단가계약 생성) → PO가 active_price로 계약단가 조회. 직무분리·승인은 PO/계약 결재선에서.
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.scm.models import (
    PriceAgreement, PriceAgreementLine, Quote, QuoteLine, Rfq, RfqLine,
)

_audit = AuditService()


class RfqService:
    async def create_rfq(self, session: AsyncSession, *, actor: int, title: str,
                         lines: list[dict], category: str | None = None,
                         due_on: date | None = None, note: str | None = None) -> Rfq:
        if not lines:
            raise ValidationError("RFQ 품목 라인이 없습니다")
        rfq = Rfq(title=title, category=category, due_on=due_on, status="DRAFT",
                  note=note, created_by=actor)
        session.add(rfq)
        await session.flush()
        for ln in lines:
            session.add(RfqLine(rfq_id=rfq.id, item_id=ln["item_id"], qty=ln["qty"],
                                spec=ln.get("spec")))
        await _audit.record_audit(session, actor=actor, action="CREATE", entity="scm.rfqs",
                                  target_id=str(rfq.id), after={"title": title, "lines": len(lines)})
        return rfq

    async def open_rfq(self, session: AsyncSession, *, actor: int, rfq_id: int) -> Rfq:
        rfq = await self._get(session, rfq_id)
        if rfq.status != "DRAFT":
            raise ValidationError(f"개찰할 수 없는 상태입니다 ({rfq.status})")
        rfq.status = "OPEN"
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity="scm.rfqs",
                                  target_id=str(rfq_id), after={"status": "OPEN"})
        return rfq

    async def submit_quote(self, session: AsyncSession, *, actor: int, rfq_id: int,
                           supplier_id: int, lines: list[dict],
                           submitted_on: date | None = None, note: str | None = None) -> Quote:
        rfq = await self._get(session, rfq_id)
        if rfq.status != "OPEN":
            raise ValidationError(f"견적 접수 가능한 상태가 아닙니다 ({rfq.status})")
        if not lines:
            raise ValidationError("견적 라인이 없습니다")
        # RFQ 수량 기준 총액 산정(견적 단가 × RFQ 수량). 견적은 RFQ 전 품목을 1회씩 커버해야 함.
        rfq_qty = {ln.item_id: float(ln.qty) for ln in (await session.scalars(
            select(RfqLine).where(RfqLine.rfq_id == rfq_id))).all()}
        seen: set[int] = set()
        for ln in lines:
            iid = ln["item_id"]
            if iid not in rfq_qty:
                raise ValidationError("RFQ에 없는 품목은 견적할 수 없습니다", {"item_id": iid})
            if iid in seen:
                raise ValidationError("견적에 중복 품목이 있습니다", {"item_id": iid})
            seen.add(iid)
            if float(ln["unit_price"]) < 0:
                raise ValidationError("견적 단가는 음수일 수 없습니다")
        missing = set(rfq_qty) - seen
        if missing:    # 일부 품목 누락 → 총액 과소·랭킹 왜곡 방지(전 품목 견적 필수)
            raise ValidationError("RFQ 전 품목을 견적해야 합니다", {"missing": sorted(missing)})
        total = 0.0
        q = Quote(rfq_id=rfq_id, supplier_id=supplier_id, status="SUBMITTED",
                  submitted_on=submitted_on or date.today(), note=note)
        session.add(q)
        await session.flush()
        for ln in lines:
            up = float(ln["unit_price"])
            session.add(QuoteLine(quote_id=q.id, item_id=ln["item_id"], unit_price=up,
                                  lead_time_days=ln.get("lead_time_days")))
            total += up * rfq_qty[ln["item_id"]]
        q.total = round(total)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE", entity="scm.quotes",
                                  target_id=str(q.id), after={"rfq": rfq_id,
                                                              "supplier": supplier_id,
                                                              "total": float(q.total)})
        return q

    async def compare(self, session: AsyncSession, *, rfq_id: int) -> list[dict]:
        """RFQ 견적 비교 — 총액 오름차순(최저가 우선). 동점은 견적 id 순."""
        quotes = (await session.scalars(select(Quote).where(
            Quote.rfq_id == rfq_id).order_by(Quote.total, Quote.id))).all()
        return [{"quote_id": q.id, "supplier_id": q.supplier_id, "total": float(q.total),
                 "status": q.status, "rank": i + 1} for i, q in enumerate(quotes)]

    async def award(self, session: AsyncSession, *, actor: int, rfq_id: int, quote_id: int,
                    valid_from: date, valid_to: date, title: str | None = None) -> PriceAgreement:
        """낙찰 — 견적을 AWARDED로, 견적 단가로 단가계약(price_agreement) 생성. RFQ AWARDED."""
        rfq = await self._get(session, rfq_id)
        if rfq.status not in ("OPEN", "CLOSED"):
            raise ValidationError(f"낙찰 가능한 상태가 아닙니다 ({rfq.status})")
        q = await session.get(Quote, quote_id)
        if q is None or q.rfq_id != rfq_id:
            raise ValidationError("해당 RFQ의 견적이 아닙니다")
        if valid_to <= valid_from:
            raise ValidationError("단가계약 종료일은 시작일 이후여야 합니다")
        q.status = "AWARDED"
        # 나머지 견적 반려
        for other in (await session.scalars(select(Quote).where(
                Quote.rfq_id == rfq_id, Quote.id != quote_id))).all():
            other.status = "REJECTED"
        pa = PriceAgreement(supplier_id=q.supplier_id, title=title or rfq.title, rfq_id=rfq_id,
                            valid_from=valid_from, valid_to=valid_to, status="ACTIVE",
                            created_by=actor)
        session.add(pa)
        await session.flush()
        for ql in (await session.scalars(select(QuoteLine).where(
                QuoteLine.quote_id == quote_id))).all():
            session.add(PriceAgreementLine(agreement_id=pa.id, item_id=ql.item_id,
                                           unit_price=ql.unit_price))
        rfq.status, rfq.awarded_quote_id = "AWARDED", quote_id
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="AWARD",
                                  entity="scm.price_agreements", target_id=str(pa.id),
                                  after={"rfq": rfq_id, "quote": quote_id,
                                         "supplier": q.supplier_id})
        return pa

    async def list_rfqs(self, session: AsyncSession, *, status: str | None = None) -> list[dict]:
        """RFQ 목록 — 최신순. 라인·견적 건수 포함(단일 group-by·N+1 회피)."""
        from sqlalchemy import func
        q = select(Rfq).order_by(Rfq.id.desc())
        if status:
            q = q.where(Rfq.status == status)
        rows = (await session.execute(q.limit(300))).scalars().all()
        ids = [r.id for r in rows]
        line_cnt = dict((await session.execute(
            select(RfqLine.rfq_id, func.count()).where(RfqLine.rfq_id.in_(ids))
            .group_by(RfqLine.rfq_id))).all()) if ids else {}
        quote_cnt = dict((await session.execute(
            select(Quote.rfq_id, func.count()).where(Quote.rfq_id.in_(ids))
            .group_by(Quote.rfq_id))).all()) if ids else {}
        return [{"id": r.id, "title": r.title, "category": r.category,
                 "due_on": str(r.due_on) if r.due_on else None, "status": r.status,
                 "awarded_quote_id": r.awarded_quote_id,
                 "line_count": int(line_cnt.get(r.id, 0)),
                 "quote_count": int(quote_cnt.get(r.id, 0))} for r in rows]

    async def detail(self, session: AsyncSession, *, rfq_id: int) -> dict:
        """RFQ 상세 — 헤더·요청 라인·접수 견적(총액)."""
        rfq = await self._get(session, rfq_id)
        lines = (await session.scalars(select(RfqLine).where(RfqLine.rfq_id == rfq_id))).all()
        quotes = (await session.scalars(select(Quote).where(
            Quote.rfq_id == rfq_id).order_by(Quote.total, Quote.id))).all()
        # 총액만 반환하면 **최저가만 보이고 납기가 안 보여** "싸지만 3개월 걸리는" 견적을 낙찰한다.
        # 품목별 단가도 없어 총액 근소차 견적의 우열을 가릴 수 없었다(QuoteLine에 다 있는 값).
        qlines: dict[int, list] = {}
        if quotes:
            for ql in (await session.scalars(select(QuoteLine).where(
                    QuoteLine.quote_id.in_([q.id for q in quotes])))).all():
                qlines.setdefault(ql.quote_id, []).append(ql)

        def _q(q):
            lns = qlines.get(q.id, [])
            leads = [int(x.lead_time_days) for x in lns if x.lead_time_days is not None]
            return {"quote_id": q.id, "supplier_id": q.supplier_id,
                    "total": float(q.total), "status": q.status,
                    "max_lead_time_days": max(leads) if leads else None,
                    "lines": [{"item_id": x.item_id, "unit_price": float(x.unit_price),
                               "lead_time_days": x.lead_time_days} for x in lns]}

        return {"id": rfq.id, "title": rfq.title, "category": rfq.category,
                "due_on": str(rfq.due_on) if rfq.due_on else None, "status": rfq.status,
                "note": rfq.note, "awarded_quote_id": rfq.awarded_quote_id,
                "lines": [{"item_id": ln.item_id, "qty": float(ln.qty), "spec": ln.spec}
                          for ln in lines],
                "quotes": [_q(q) for q in quotes]}

    async def cancel_rfq(self, session: AsyncSession, *, actor: int, rfq_id: int) -> Rfq:
        rfq = await self._get(session, rfq_id)
        if rfq.status == "AWARDED":
            raise ValidationError("이미 낙찰된 RFQ는 취소할 수 없습니다")
        rfq.status = "CANCELED"
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity="scm.rfqs",
                                  target_id=str(rfq_id), after={"status": "CANCELED"})
        return rfq

    async def _get(self, session: AsyncSession, rfq_id: int) -> Rfq:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise ValidationError("존재하지 않는 RFQ입니다")
        return rfq


class PriceAgreementService:
    async def active_price(self, session: AsyncSession, *, item_id: int, supplier_id: int,
                           on: date | None = None) -> float | None:
        """유효한 단가계약의 품목 계약단가 — 없으면 None. (공급사·품목·유효기간 기준)."""
        on = on or date.today()
        row = await session.scalar(
            select(PriceAgreementLine.unit_price)
            .join(PriceAgreement, PriceAgreement.id == PriceAgreementLine.agreement_id)
            .where(PriceAgreement.supplier_id == supplier_id,
                   PriceAgreementLine.item_id == item_id,
                   PriceAgreement.status == "ACTIVE",
                   PriceAgreement.valid_from <= on, PriceAgreement.valid_to >= on)
            .order_by(PriceAgreement.valid_from.desc()))
        return float(row) if row is not None else None

    async def list_active(self, session: AsyncSession, *, on: date | None = None) -> list[dict]:
        on = on or date.today()
        rows = (await session.scalars(select(PriceAgreement).where(
            PriceAgreement.status == "ACTIVE", PriceAgreement.valid_from <= on,
            PriceAgreement.valid_to >= on).order_by(PriceAgreement.valid_to))).all()
        palines: dict[int, list] = {}
        if rows:
            for pl in (await session.scalars(select(PriceAgreementLine).where(
                    PriceAgreementLine.agreement_id.in_([p.id for p in rows])))).all():
                palines.setdefault(pl.agreement_id, []).append(pl)
        # PO 생성 시 이 단가가 자동 적용되는데(po_service) 계약단가를 확인할 경로가 없었다.
        return [{"id": p.id, "supplier_id": p.supplier_id, "title": p.title,
                 "valid_from": p.valid_from, "valid_to": p.valid_to, "rfq_id": p.rfq_id,
                 "lines": [{"item_id": x.item_id, "unit_price": float(x.unit_price)}
                           for x in palines.get(p.id, [])]}
                for p in rows]
