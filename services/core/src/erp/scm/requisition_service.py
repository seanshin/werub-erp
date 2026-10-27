"""구매요청(PR)·내부 자재청구 — 요청→승인(SoD)→실행 앞단 (내부운영 갭 감사 Tier2·마이그 0217).

audit 발견: PO가 수요 청구 앞단 없이 REQUESTED로 바로 시작·재고 출고(issue)는 무승인 즉시 OUT.
- 구매요청: 부서 수요 기안→승인→PO 전환(po_service.request 재사용). 승인 후에만 발주.
- 자재청구: 병동/부서 불출 요청→승인→FIFO 출고(stock_service.issue). 승인 후에만 출고.
직무분리: 요청자 ≠ 승인자.
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ForbiddenError, ValidationError
from erp.scm.models import (
    Item, MaterialRequest, PurchaseRequisition, RequisitionLine,
)

_audit = AuditService()


def _assert_dept(dept_only: bool, approver_dept: int | None, target_dept: int | None) -> None:
    """부서 경계(감사 H2) — DEPT 스코프 승인자는 본부서 요청만 처리(휴가 decide와 동형)."""
    from erp.errors import ForbiddenError
    if dept_only and target_dept != approver_dept:
        raise ForbiddenError("본인 부서의 요청만 처리할 수 있습니다")


class PurchaseRequisitionService:
    async def _next_no(self, session: AsyncSession, year: int) -> str:
        from erp.common.doc_no import lock_doc_no
        await lock_doc_no(session, f"구매요청-{year}")   # 동시 기안 채번 경합 직렬화(v1.204)
        n = int(await session.scalar(
            select(func.count()).select_from(PurchaseRequisition).where(
                PurchaseRequisition.req_no.like(f"구매요청-{year}-%"))) or 0) + 1
        return f"구매요청-{year}-{n:04d}"

    async def create(self, session: AsyncSession, *, requester: int,
                     department_id: int | None, lines: list[dict], reason: str | None = None,
                     needed_by: date | None = None, on: date | None = None) -> PurchaseRequisition:
        if not lines:
            raise ValidationError("구매요청 라인이 없습니다")
        ids = {ln["item_id"] for ln in lines}
        found = set((await session.scalars(select(Item.id).where(Item.id.in_(ids)))).all())
        if ids - found:
            raise ValidationError("존재하지 않는 품목입니다", {"item_ids": sorted(ids - found)})
        req = PurchaseRequisition(
            req_no=await self._next_no(session, (on or date.today()).year),
            department_id=department_id, requester=requester, reason=reason,
            needed_by=needed_by, status="PENDING")
        session.add(req)
        await session.flush()
        for ln in lines:
            if float(ln["qty"]) <= 0:
                raise ValidationError("요청 수량은 0보다 커야 합니다", {"item_id": ln["item_id"]})
            session.add(RequisitionLine(req_id=req.id, item_id=ln["item_id"], qty=ln["qty"],
                                        est_price=ln.get("est_price"), note=ln.get("note")))
        await session.flush()
        return req

    async def _get(self, session: AsyncSession, req_id: int) -> PurchaseRequisition:
        r = await session.get(PurchaseRequisition, req_id, with_for_update=True)
        if r is None:
            raise ValidationError("존재하지 않는 구매요청입니다", {"req_id": req_id})
        return r

    async def decide(self, session: AsyncSession, *, req_id: int, approve: bool,
                     actor: int, reason: str = "", dept_only: bool = False,
                     approver_dept: int | None = None) -> PurchaseRequisition:
        r = await self._get(session, req_id)
        if r.status != "PENDING":
            raise ValidationError("승인 가능한 상태가 아닙니다", {"status": r.status})
        if r.requester == actor:
            raise ValidationError("직무분리 — 요청자 본인은 승인할 수 없습니다")
        _assert_dept(dept_only, approver_dept, r.department_id)  # 감사 H2 — 부서 경계 집행
        r.status = "APPROVED" if approve else "REJECTED"
        r.approved_by = actor
        await _audit.record_audit(session, actor=actor, action="APPROVE" if approve else "REJECT",
                                  entity="scm.purchase_requisitions", target_id=str(req_id),
                                  after={"status": r.status, "reason": reason})
        return r

    async def cancel(self, session: AsyncSession, *, req_id: int, requester: int
                     ) -> PurchaseRequisition:
        r = await self._get(session, req_id)
        if r.requester != requester:
            raise ForbiddenError("본인 요청만 회수할 수 있습니다")
        if r.status not in ("PENDING",):
            raise ValidationError("회수 가능한 상태가 아닙니다", {"status": r.status})
        r.status = "CANCELED"
        return r

    async def convert_to_po(self, session: AsyncSession, *, req_id: int, supplier_id: int,
                            unit_prices: dict, actor: int, dept_only: bool = False,
                            approver_dept: int | None = None):
        """승인된 구매요청 → 발주 전환. 요청 라인 수량 + 지정 단가로 PO 생성(po_service 재사용)."""
        from erp.scm.po_service import PurchaseOrderService
        r = await self._get(session, req_id)
        if r.status != "APPROVED":
            raise ValidationError("승인된 요청만 발주 전환할 수 있습니다", {"status": r.status})
        _assert_dept(dept_only, approver_dept, r.department_id)  # 감사 H2
        rlines = (await session.scalars(
            select(RequisitionLine).where(RequisitionLine.req_id == req_id))).all()
        prices = {int(k): float(v) for k, v in (unit_prices or {}).items()}
        po_lines = []
        for ln in rlines:
            price = prices.get(ln.item_id, float(ln.est_price) if ln.est_price else None)
            if price is None:
                raise ValidationError("품목 단가가 필요합니다", {"item_id": ln.item_id})
            po_lines.append({"item_id": ln.item_id, "qty": float(ln.qty), "unit_price": price})
        po = await PurchaseOrderService().request(
            session, supplier_id=supplier_id, actor=actor, lines=po_lines,
            department_id=r.department_id)
        r.status, r.po_id = "CONVERTED", po.id
        await _audit.record_audit(session, actor=actor, action="CONVERT",
                                  entity="scm.purchase_requisitions", target_id=str(req_id),
                                  after={"po_id": po.id})
        return {"req_id": req_id, "po_id": po.id, "status": r.status}

    async def get_detail(self, session: AsyncSession, req_id: int) -> dict:
        """구매요청 상세 — 헤더+라인(품목명 조인). FE 상세/발주전환 화면 소스."""
        r = await session.get(PurchaseRequisition, req_id)
        if r is None:
            raise ValidationError("존재하지 않는 구매요청입니다", {"req_id": req_id})
        rows = (await session.execute(
            select(RequisitionLine, Item.name, Item.code)
            .join(Item, Item.id == RequisitionLine.item_id)
            .where(RequisitionLine.req_id == req_id).order_by(RequisitionLine.id))).all()
        return {"id": r.id, "req_no": r.req_no, "department_id": r.department_id,
                "requester": r.requester, "reason": r.reason, "needed_by": r.needed_by,
                "status": r.status, "po_id": r.po_id,
                "lines": [{"item_id": ln.item_id, "item_code": code, "item_name": name,
                           "qty": float(ln.qty),
                           "est_price": float(ln.est_price) if ln.est_price is not None else None,
                           "note": ln.note} for ln, name, code in rows]}

    async def list_requisitions(self, session: AsyncSession, *, status: str | None = None,
                                department_id: int | None = None) -> list[dict]:
        q = select(PurchaseRequisition)
        if status:
            q = q.where(PurchaseRequisition.status == status)
        if department_id is not None:
            q = q.where(PurchaseRequisition.department_id == department_id)
        rows = (await session.scalars(
            q.order_by(PurchaseRequisition.id.desc()).limit(500))).all()
        return [{"id": r.id, "req_no": r.req_no, "department_id": r.department_id,
                 "requester": r.requester, "reason": r.reason, "needed_by": r.needed_by,
                 "status": r.status, "po_id": r.po_id} for r in rows]


class MaterialRequestService:
    async def _next_no(self, session: AsyncSession, year: int) -> str:
        from erp.common.doc_no import lock_doc_no
        await lock_doc_no(session, f"자재청구-{year}")   # 동시 청구 채번 경합 직렬화(v1.204)
        n = int(await session.scalar(
            select(func.count()).select_from(MaterialRequest).where(
                MaterialRequest.req_no.like(f"자재청구-{year}-%"))) or 0) + 1
        return f"자재청구-{year}-{n:04d}"

    async def create(self, session: AsyncSession, *, requester: int, department_id: int | None,
                     item_id: int, qty: float, reason: str | None = None,
                     on: date | None = None) -> MaterialRequest:
        if float(qty) <= 0:
            raise ValidationError("청구 수량은 0보다 커야 합니다")
        if await session.get(Item, item_id) is None:
            raise ValidationError("존재하지 않는 품목입니다", {"item_id": item_id})
        req = MaterialRequest(
            req_no=await self._next_no(session, (on or date.today()).year),
            department_id=department_id, requester=requester, item_id=item_id, qty=qty,
            reason=reason, status="REQUESTED")
        session.add(req)
        await session.flush()
        return req

    async def _get(self, session: AsyncSession, req_id: int) -> MaterialRequest:
        r = await session.get(MaterialRequest, req_id, with_for_update=True)
        if r is None:
            raise ValidationError("존재하지 않는 자재청구입니다", {"req_id": req_id})
        return r

    async def decide(self, session: AsyncSession, *, req_id: int, approve: bool,
                     actor: int, reason: str = "", dept_only: bool = False,
                     approver_dept: int | None = None) -> MaterialRequest:
        r = await self._get(session, req_id)
        if r.status != "REQUESTED":
            raise ValidationError("승인 가능한 상태가 아닙니다", {"status": r.status})
        if r.requester == actor:
            raise ValidationError("직무분리 — 요청자 본인은 승인할 수 없습니다")
        _assert_dept(dept_only, approver_dept, r.department_id)  # 감사 H2
        r.status = "APPROVED" if approve else "REJECTED"
        r.approved_by = actor
        await _audit.record_audit(  # v1.203 — PR decide와 대칭(감사로그 부재 보완)
            session, actor=actor, action="APPROVE" if approve else "REJECT",
            entity="scm.material_requests", target_id=str(req_id),
            after={"status": r.status, "reason": reason or None})
        return r

    async def cancel(self, session: AsyncSession, *, req_id: int, requester: int
                     ) -> MaterialRequest:
        r = await self._get(session, req_id)
        if r.requester != requester:
            raise ForbiddenError("본인 청구만 회수할 수 있습니다")
        if r.status != "REQUESTED":
            raise ValidationError("회수 가능한 상태가 아닙니다", {"status": r.status})
        r.status = "CANCELED"
        return r

    async def issue(self, session: AsyncSession, *, req_id: int, actor: int,
                    moved_on: date | None = None, dept_only: bool = False,
                    approver_dept: int | None = None) -> dict:
        """승인된 자재청구 → FIFO 출고(stock_service 재사용). 승인 후에만 출고(무승인 즉시OUT 갭 보완)."""
        from erp.scm.stock_service import StockService
        r = await self._get(session, req_id)
        if r.status != "APPROVED":
            raise ValidationError("승인된 청구만 불출할 수 있습니다", {"status": r.status})
        _assert_dept(dept_only, approver_dept, r.department_id)  # 감사 H2
        moved_on = moved_on or date.today()
        await StockService().issue(session, item_id=r.item_id, qty=float(r.qty),
                                   moved_on=moved_on, actor=actor)
        r.status, r.issued_on = "ISSUED", moved_on
        await _audit.record_audit(session, actor=actor, action="ISSUE",
                                  entity="scm.material_requests", target_id=str(req_id),
                                  after={"item_id": r.item_id, "qty": float(r.qty)})
        return {"req_id": req_id, "status": r.status, "item_id": r.item_id,
                "qty": float(r.qty), "issued_on": moved_on.isoformat()}

    async def list_requests(self, session: AsyncSession, *, status: str | None = None,
                            department_id: int | None = None) -> list[dict]:
        q = select(MaterialRequest)
        if status:
            q = q.where(MaterialRequest.status == status)
        if department_id is not None:
            q = q.where(MaterialRequest.department_id == department_id)
        rows = (await session.scalars(q.order_by(MaterialRequest.id.desc()).limit(500))).all()
        return [{"id": r.id, "req_no": r.req_no, "department_id": r.department_id,
                 "requester": r.requester, "item_id": r.item_id, "qty": float(r.qty),
                 "reason": r.reason, "status": r.status, "issued_on": r.issued_on} for r in rows]
