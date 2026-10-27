"""SCM 라우터 — design §4.3."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.policy import require_license, require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.errors import ForbiddenError, ValidationError
from erp.scm.models import Item, Supplier
from erp.scm.narcotics_service import NarcoticsService
from erp.scm.po_service import PurchaseOrderService
from erp.scm.safety_service import DrugRecallService, MedicalWasteService
from erp.scm.vendor_service import VendorService
from erp.scm.stock_service import StockService
from erp.common.kst import kst_iso

router = APIRouter(prefix="/inventory", tags=["scm"])
_stock, _narc, _po = StockService(), NarcoticsService(), PurchaseOrderService()
_vendor = VendorService()
_recall, _waste = DrugRecallService(), MedicalWasteService()
_audit = AuditService()


def _require(user: CurrentUser, resource: str, action: str):
    """분석 H3/M2 — 기본값 제거, 리소스·액션 명시 강제."""
    return require_scope(user, resource, action)


class ItemBody(BaseModel):
    code: str
    name: str
    category: str
    unit: str = "EA"
    is_narcotic: bool = False
    safety_stock_qty: float = 0
    barcode: str | None = None




def _handler_label(handler_id: int | None) -> str:
    """취급자 표기 — 음수는 작업대 일일 출입증(-출입증 id·3차 검토 G). 사용자 id와 혼동되지 않게."""
    if handler_id is None:
        return ""
    return f"작업대 출입증 #{-handler_id}" if handler_id < 0 else f"사용자 #{handler_id}"

@router.post("/items", status_code=201)
async def create_item(body: ItemBody, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "scm.items", "write")
    item = Item(**body.model_dump())
    session.add(item)
    await session.flush()
    return {"id": item.id}


@router.get("/items")
async def list_items(user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session),
                     page: int = 1, size: int = 50):
    _require(user, "scm.items", "read")  # 분석 H3 — 누락이던 검증
    size = min(size, 1000)   # v1.203 — 콤보박스 소스 200 하드캡으로 초과 품목 선택 불가하던 상한 완화
    rows = (await session.execute(
        select(Item).order_by(Item.id).offset((page - 1) * size).limit(size))).scalars().all()
    # N+1 제거 (분석 M1) — 재고를 단일 group-by로 일괄 조회
    from sqlalchemy import func

    from erp.scm.models import Lot
    stock_map = dict((await session.execute(
        select(Lot.item_id, func.coalesce(func.sum(Lot.remaining_qty), 0))
        .where(Lot.item_id.in_([it.id for it in rows]))
        .group_by(Lot.item_id))).all()) if rows else {}
    items = [{"id": it.id, "code": it.code, "barcode": it.barcode, "name": it.name,
              "is_narcotic": it.is_narcotic,
              "stock": float(stock_map.get(it.id, 0)),
              "safety_stock": float(it.safety_stock_qty)} for it in rows]
    return {"items": items, "page": page, "size": size}



@router.get("/requestable-items")
async def list_requestable_items(user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """청구용 품목 목록 — 자재청구·구매요청 화면의 품목 선택 소스(전 직원).

    2026-10-09 시나리오 S-G: 두 화면은 전 직원 메뉴인데 품목 선택이 `/items`(scm.items read — 직원 불가)를
    불러 직원이 품목을 고를 수 없었다. scm.items read를 직원에게 열면 평가·원가·로트 단가까지 열리므로,
    청구 권한(scm.requisition read)으로 **선택에 필요한 필드만**(단가·공급사·로트 없음) 돌려준다.
    """
    from sqlalchemy import func

    from erp.scm.models import Lot
    _require(user, "scm.requisition", "read")
    # 마약류 재고량은 scm.narcotics read 권한자만 — 청구 권한(전 직원)으로 전원에게 보이면 안 된다(시나리오 2회차 검토)
    from erp.common.policy import Role, ScopeKind, resolve_scope
    see_narcotic_stock = resolve_scope("scm.narcotics", "read", role=Role(user.role),
                                       dept_id=user.dept_id, user_id=user.user_id).kind != ScopeKind.NONE
    rows = (await session.execute(select(Item).order_by(Item.name).limit(5000))).scalars().all()
    stock_map = dict((await session.execute(
        select(Lot.item_id, func.coalesce(func.sum(Lot.remaining_qty), 0))
        .group_by(Lot.item_id))).all()) if rows else {}
    return {"items": [{"id": it.id, "code": it.code, "name": it.name, "unit": it.unit,
                       "is_narcotic": it.is_narcotic,
                       "stock": (None if it.is_narcotic and not see_narcotic_stock
                                 else float(stock_map.get(it.id, 0))),
                       "safety_stock": float(it.safety_stock_qty)} for it in rows]}

class ItemPatchBody(BaseModel):
    barcode: str | None = None
    safety_stock_qty: float | None = None
    par_level: float | None = None        # 보충 목표(order-up-to)·자동발주 제안


@router.patch("/items/{item_id}")
async def patch_item(item_id: int, body: ItemPatchBody,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """품목 수정 — 바코드 등록·안전재고/보충목표(par) 조정(워크스테이션)."""
    _require(user, "scm.items", "write")
    item = await session.get(Item, item_id)
    if item is None:
        raise ValidationError("존재하지 않는 품목입니다")
    if body.barcode is not None:
        item.barcode = body.barcode or None
    if body.safety_stock_qty is not None:
        item.safety_stock_qty = body.safety_stock_qty
    if body.par_level is not None:
        item.par_level = body.par_level if body.par_level > 0 else None
    return {"id": item.id, "barcode": item.barcode}


# ── 안전재고 자동발주(par-level 보충·P3-7) ──────────────────────
def _replenish():
    from erp.scm.replenishment_service import ReplenishmentService
    return ReplenishmentService()


@router.get("/replenishment/suggestions")
async def replenishment_suggestions(user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """재주문점(안전재고) 미만 품목 + par-level 보충 제안 수량."""
    _require(user, "scm.items", "read")
    return await _replenish().suggestions(session)


class ReplenishBody(BaseModel):
    item_ids: list[int]
    department_id: int | None = None
    qty_override: dict[int, float] | None = None


@router.post("/replenishment/create-pr", status_code=201)
async def replenishment_create_pr(body: ReplenishBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """선택 품목 보충을 1건 구매요청(PR)으로 생성 — 이후 승인·발주는 PR 흐름."""
    _require(user, "scm.po", "write")
    req = await _replenish().create_pr(session, item_ids=body.item_ids, requester=user.user_id,
                                       department_id=body.department_id,
                                       qty_override=body.qty_override)
    return {"id": req.id, "req_no": req.req_no, "status": req.status}


@router.get("/items/lookup")
async def lookup_item(q: str, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """스캔 조회 — barcode(우선)/code 정확일치로 품목+재고+로트 반환. 워크스테이션."""
    from sqlalchemy import func

    from erp.scm.models import Lot
    _require(user, "scm.items", "read")
    item = (await session.execute(
        select(Item).where((Item.barcode == q) | (Item.code == q)).limit(1)
    )).scalar_one_or_none()
    if item is None:
        raise ValidationError(f"품목을 찾을 수 없습니다: {q}")
    stock = float((await session.execute(
        select(func.coalesce(func.sum(Lot.remaining_qty), 0))
        .where(Lot.item_id == item.id))).scalar() or 0)
    lots = (await session.execute(
        select(Lot).where(Lot.item_id == item.id, Lot.remaining_qty > 0)
        .order_by(Lot.expiry_on))).scalars().all()
    return {
        "item": {"id": item.id, "code": item.code, "barcode": item.barcode, "name": item.name,
                 "unit": item.unit, "is_narcotic": item.is_narcotic,
                 "safety_stock": float(item.safety_stock_qty)},
        "stock": stock, "below_safety": stock < float(item.safety_stock_qty),
        "lots": [{"lot_no": lot.lot_no, "expiry_on": lot.expiry_on,
                  "remaining": float(lot.remaining_qty)} for lot in lots]}


class ReceiveBody(BaseModel):
    item_id: int
    lot_no: str
    expiry_on: date
    qty: float
    moved_on: date


@router.post("/movements/receive", status_code=201)
async def receive_stock(body: ReceiveBody, user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "scm.items", "write")
    lot = await _stock.receive(session, actor=user.user_id, **body.model_dump())
    return {"lot_id": lot.id}


class IssueBody(BaseModel):
    item_id: int
    qty: float
    moved_on: date
    override_expiry: bool = False
    override_reason: str | None = None


@router.post("/movements/issue", status_code=201)
async def issue_stock(body: IssueBody, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "scm.items", "write")
    if body.override_expiry and user.role != "ADMIN":
        raise ForbiddenError("임박 lot 강제 출고는 관리자만 가능합니다")
    movements = await _stock.issue(session, actor=user.user_id, **body.model_dump())
    return {"movements": [{"lot_id": m.lot_id, "qty": float(m.qty)} for m in movements]}


# ── 마약류 (FR-20/21 — append-only) ────────────────────────
class NarcoticEntryBody(BaseModel):
    item_id: int
    entry_type: str   # IN | OUT | DISPOSE | ADJUST
    qty: float
    lot_id: int | None = None
    entry_on: date | None = None   # 거래일(미지정 시 오늘) — 월 수불일지 귀속


@router.post("/narcotics/entries", status_code=201)
async def narcotic_entry(body: NarcoticEntryBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _require(user, "scm.narcotics", "write")
    require_license(user)   # FR-47 — 면허 만료자 마약류 취급 차단
    # 작업대(kiosk·일일 PIN)는 마약류 **입고(IN)만** — 출고·폐기·조정은 마약류관리법상 면허 취급자 필요
    # (사용자 결정: 입고 전용). WORKSTATION은 license_flag 하드코딩이라 위 면허 게이트를 통과하므로
    # 여기서 entry_type을 별도 제한(적대검토 관찰).
    if user.role == "WORKSTATION" and body.entry_type != "IN":
        raise ForbiddenError("작업대(kiosk)는 마약류 입고(IN)만 가능합니다 — 출고·폐기·조정·정정은 면허 사용자")
    entry = await _narc.add_entry(session, handler_id=user.user_id, **body.model_dump())
    return {"id": entry.id, "seq_no": entry.seq_no, "balance": float(entry.balance_after)}


class NarcoticCorrectBody(BaseModel):
    corrected_qty: float
    reason: str
    entry_on: date | None = None   # 정정 거래일(미지정 시 오늘)


@router.post("/narcotics/entries/{entry_id}/correct", status_code=201)
async def narcotic_correct(entry_id: int, body: NarcoticCorrectBody,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """정정 레코드 — balance는 시스템 산출 (수량 입력 불가, design §3.4)."""
    _require(user, "scm.narcotics", "write")
    require_license(user)   # FR-47 — 면허 만료자 마약류 정정 차단
    if user.role == "WORKSTATION":   # 작업대는 입고 전용 — 정정 불가(면허 사용자)
        raise ForbiddenError("작업대(kiosk)는 마약류 정정 불가 — 면허 사용자가 수행")
    entry = await _narc.correct(session, original_id=entry_id, handler_id=user.user_id,
                                corrected_qty=body.corrected_qty, reason=body.reason,
                                entry_on=body.entry_on)
    return {"id": entry.id, "seq_no": entry.seq_no, "balance": float(entry.balance_after)}


@router.get("/narcotics/ledger")
async def narcotic_ledger(item_id: int, yyyymm: str,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """수불일지 (FR-21) — 조회·반출은 접속기록."""
    _require(user, "scm.narcotics", "read")
    rows = await _narc.monthly_ledger(session, item_id=item_id, yyyymm=yyyymm)
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="scm.narcotics_ledger", target_id=str(item_id),
                               route="/inventory/narcotics/ledger")
    return {"items": [{"id": r.id, "seq_no": r.seq_no, "entry_type": r.entry_type,
                       "qty": float(r.qty), "balance": float(r.balance_after),
                       "handler_id": r.handler_id, "handler_label": _handler_label(r.handler_id),
                       "corrects_id": r.corrects_id,
                       "at": r.created_at} for r in rows]}


@router.get("/narcotics/ledger/export")
async def narcotic_ledger_export(item_id: int, yyyymm: str,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """수불일지 Excel (FR-21 — 월별 보고용). 반출 접속기록."""
    _require(user, "scm.narcotics", "read")
    from fastapi.responses import StreamingResponse

    from erp.common.excel import build_xlsx
    rows = await _narc.monthly_ledger(session, item_id=item_id, yyyymm=yyyymm)
    await _audit.record_access(session, actor=user.user_id, action="EXPORT",
                               entity="scm.narcotics_ledger", target_id=str(item_id),
                               route="/inventory/narcotics/ledger/export")
    content = build_xlsx(
        sheet_name=f"수불일지 {yyyymm}",
        headers=["순번", "구분", "수량", "잔량", "취급자", "원본참조", "정정사유", "일시"],
        rows=[[r.seq_no, r.entry_type, float(r.qty), float(r.balance_after),
               _handler_label(r.handler_id), r.corrects_id, r.correction_reason, r.created_at]
              for r in rows])
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="narcotics-{item_id}-{yyyymm}.xlsx"'})


# ── 공급업체·발주 (FR-14) ──────────────────────────────────
class SupplierBody(BaseModel):
    name: str
    business_no: str
    contact: str | None = None


@router.post("/suppliers", status_code=201)
async def create_supplier(body: SupplierBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    _require(user, "scm.po", "write")
    s = Supplier(**body.model_dump())
    session.add(s)
    await session.flush()
    return {"id": s.id}


@router.get("/suppliers")
async def list_suppliers(active_only: bool = True,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """공급사 목록 — 발주·위탁재고·반품 화면의 거래처 선택(드롭다운) 소스."""
    _require(user, "scm.po", "read")
    q = select(Supplier).order_by(Supplier.name)
    if active_only:
        q = q.where(Supplier.is_active.is_(True))
    rows = (await session.execute(q.limit(500))).scalars().all()
    return {"items": [{"id": s.id, "name": s.name, "business_no": s.business_no,
                       "status": s.status, "contact": s.contact, "is_active": s.is_active,
                       "vendor_type": s.vendor_type,
                       "eval_score": float(s.eval_score) if s.eval_score is not None else None}
                      for s in rows]}


class VendorStatusBody(BaseModel):
    status: str   # PENDING|APPROVED|SUSPENDED|TERMINATED


@router.patch("/suppliers/{supplier_id}/status")
async def set_vendor_status(supplier_id: int, body: VendorStatusBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """공급사 등록심사 상태 — APPROVED만 발주 가능(is_active 동기화)."""
    _require(user, "scm.po", "write")
    sup = await _vendor.set_status(session, supplier_id=supplier_id, status=body.status,
                                   actor=user.user_id)
    return {"id": sup.id, "status": sup.status, "is_active": sup.is_active}


class VendorEvalBody(BaseModel):
    period: str           # yyyy-mm
    delivery: float       # 납기준수 0~100
    quality: float        # 검수품질 0~100
    price: float          # 단가경쟁 0~100
    note: str | None = None


@router.post("/suppliers/{supplier_id}/evaluate", status_code=201)
async def evaluate_vendor(supplier_id: int, body: VendorEvalBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """공급사 평가 — 납기·품질·단가 점수→총점(가중), 최근값 eval_score 반영."""
    _require(user, "scm.po", "write")
    ev = await _vendor.evaluate(session, supplier_id=supplier_id, period=body.period,
                                delivery=body.delivery, quality=body.quality, price=body.price,
                                note=body.note, actor=user.user_id)
    return {"supplier_id": supplier_id, "period": ev.period, "total": float(ev.total)}


@router.get("/suppliers/{supplier_id}/scorecard")
async def vendor_scorecard(supplier_id: int,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    _require(user, "scm.po", "read")
    return await _vendor.scorecard(session, supplier_id=supplier_id)


class PoRequestBody(BaseModel):
    supplier_id: int
    lines: list[dict]
    department_id: int | None = None   # 예산 귀속 부서(약정 연계)
    account_code: str = "1150"          # 예산 계정(기본 저장품)


@router.post("/purchase-orders", status_code=201)
async def request_po(body: PoRequestBody,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    _require(user, "scm.po", "write")
    po = await _po.request(session, supplier_id=body.supplier_id, actor=user.user_id,
                           lines=body.lines, department_id=body.department_id,
                           account_code=body.account_code)
    return {"id": po.id, "status": po.status}


@router.get("/purchase-orders")
async def list_pos(user: CurrentUser = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session),
                   page: int = 1, size: int = 50, status: str | None = None):
    """발주 보드 (FR-14). status=콤마 목록 필터(v1.203 — RMA 화면이 최신 N건 클라 필터로
    오래된 입고 발주를 놓치던 결함 보완)."""
    _require(user, "scm.po", "read")
    from erp.scm.models import PoLine, PurchaseOrder
    size = min(size, 200)
    q = select(PurchaseOrder)
    if status:
        q = q.where(PurchaseOrder.status.in_(
            [s.strip().upper() for s in status.split(",") if s.strip()]))
    pos = (await session.execute(
        q.order_by(PurchaseOrder.id.desc())
        .offset((page - 1) * size).limit(size))).scalars().all()
    # N+1 제거 (분석 M1) — 라인 IN-batch 조회 후 그룹핑
    lines_by_po: dict[int, list] = {}
    if pos:
        for ln in (await session.execute(
                select(PoLine).where(PoLine.po_id.in_([po.id for po in pos])))).scalars():
            lines_by_po.setdefault(ln.po_id, []).append(ln)
    # 3-way 대사의 핵심인 **미입고 잔량**이 어떤 EP에도 없어 분할입고(PARTIAL) 추적이 불가능했다.
    # 승인 판단에 쓰는 **발주 총액**도 라인 단가만 있고 합계가 없어 화면이 매번 재계산해야 했다.
    items = [{"id": po.id, "supplier_id": po.supplier_id, "status": po.status,
              "requested_at": po.requested_at,
              "department_id": po.department_id, "account_code": po.account_code,
              "total_amount": round(sum(float(ln.qty) * float(ln.unit_price)
                                        for ln in lines_by_po.get(po.id, [])), 2),
              "lines": [{"item_id": ln.item_id, "qty": float(ln.qty),
                         "unit_price": float(ln.unit_price),
                         "received_qty": float(ln.received_qty),
                         "outstanding_qty": float(ln.qty) - float(ln.received_qty)}
                        for ln in lines_by_po.get(po.id, [])]} for po in pos]
    return {"items": items, "page": page, "size": size}


@router.patch("/purchase-orders/{po_id}/approve")
async def approve_po(po_id: int, override: bool = False,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """발주 승인 — 직무분리(요청자≠승인자) + 부서 지정 시 예산 약정(encumber).

    결재선 룰 있으면 다단계. override=true는 최고경영자 전권(DIRECTOR 전용·엔진에서 재검증).
    """
    # 2026-10-09 시나리오 S-D M2: 결재선 단계 적격자(DIRECTOR 등)는 scm.po write 없이도 승인
    from erp.common.approval_engine import require_scope_or_step_approver
    await require_scope_or_step_approver(session, user, "scm.po", "write",
                                         doc_type="PO", doc_id=po_id, override=override)
    po = await _po.approve(session, po_id=po_id, actor=user.user_id, override=override)
    # 다단계 결재선에서 내 단계만 기록되고 아직 확정 전이면 REQUESTED 유지 — 그 신호를 명시.
    return {"id": po.id, "status": po.status, "pending_next_step": po.status == "REQUESTED"}


class PoRejectBody(BaseModel):
    reason: str = ""


@router.patch("/purchase-orders/{po_id}/reject")
async def reject_po(po_id: int, body: PoRejectBody | None = None,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """발주 반려 — 예산 약정 해제(가용예산 복원)."""
    from erp.common.approval_engine import require_scope_or_step_approver
    await require_scope_or_step_approver(session, user, "scm.po", "write",
                                         doc_type="PO", doc_id=po_id)
    po = await _po.reject(session, po_id=po_id, actor=user.user_id,
                          reason=(body.reason if body else ""))
    return {"id": po.id, "status": po.status}


class PoReceiveBody(BaseModel):
    received_on: date
    receipts: list[dict]   # [{item_id, lot_no, expiry_on, qty}]


@router.post("/purchase-orders/{po_id}/receive")
async def receive_po(po_id: int, body: PoReceiveBody,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    _require(user, "scm.po", "write")
    from datetime import date as _date
    receipts = [{**r, "expiry_on": _date.fromisoformat(r["expiry_on"])
                 if isinstance(r["expiry_on"], str) else r["expiry_on"]}
                for r in body.receipts]
    po = await _po.receive(session, po_id=po_id, actor=user.user_id,
                           receipts=receipts, received_on=body.received_on)
    return {"id": po.id, "status": po.status}


# ── 입고 검수·반품(RMA·마이그 0214) ─────────────────────────────
_returns = None


def _return_service():
    global _returns
    if _returns is None:
        from erp.scm.return_service import PurchaseReturnService
        _returns = PurchaseReturnService()
    return _returns


class PoReturnBody(BaseModel):
    lot_id: int
    qty: float
    reason: str                # DEFECT|DAMAGE|EXPIRY|WRONG_ITEM|OTHER
    returned_on: date
    note: str | None = None


@router.post("/purchase-orders/{po_id}/returns", status_code=201)
async def create_po_return(po_id: int, body: PoReturnBody,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """입고 반품 — 로트 잔량 차감(RETURN)·credit note(DR 2110/CR 1150)·미지급 차감·교체 재입고 재개."""
    _require(user, "scm.po", "write")
    ret = await _return_service().create_return(
        session, po_id=po_id, lot_id=body.lot_id, qty=body.qty, reason=body.reason,
        returned_on=body.returned_on, note=body.note, actor=user.user_id)
    await session.commit()
    return {"id": ret.id, "amount": float(ret.amount),
            "credit_voucher_ref": ret.credit_voucher_ref}


@router.get("/purchase-orders/{po_id}/returns")
async def list_po_returns(po_id: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    _require(user, "scm.po", "read")
    return {"items": await _return_service().list_returns(session, po_id=po_id)}


@router.get("/purchase-orders/{po_id}/returnable-lots")
async def returnable_lots(po_id: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """반품 대상 로트 — 발주 품목·해당 공급사·잔량>0 로트(RMA 화면 로트 선택 소스).

    반품은 lot_id 지정인데 로트 목록 EP가 없어 화면이 로트를 못 골랐음. 발주의 PoLine 품목과
    공급사가 일치하고 잔량 남은 로트만 노출(반품은 로트 잔량 이내)."""
    _require(user, "scm.po", "read")
    from erp.scm.models import Item, Lot, PoLine, PurchaseOrder
    po = await session.get(PurchaseOrder, po_id)
    if po is None:
        raise ValidationError("존재하지 않는 발주입니다", {"po_id": po_id})
    item_ids = (await session.execute(
        select(PoLine.item_id).where(PoLine.po_id == po_id))).scalars().all()
    if not item_ids:
        return {"items": []}
    lots = (await session.execute(
        select(Lot).where(Lot.item_id.in_(item_ids), Lot.supplier_id == po.supplier_id,
                          Lot.remaining_qty > 0).order_by(Lot.item_id, Lot.id))).scalars().all()
    name_map = dict((await session.execute(
        select(Item.id, Item.name).where(Item.id.in_(item_ids)))).all())
    return {"items": [{"id": lot.id, "item_id": lot.item_id,
                       "item_name": name_map.get(lot.item_id, ""), "lot_no": lot.lot_no,
                       "remaining_qty": float(lot.remaining_qty),
                       "unit_cost": float(lot.unit_cost) if lot.unit_cost is not None else None,
                       "expiry_on": lot.expiry_on} for lot in lots]}


# ── 재고자산 평가·안전재고 부족 (FR-13/46 확장) ──────────────────
@router.get("/valuation")
async def inventory_valuation(user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """재고자산 평가 — 품목별 잔량×매입단가, 카테고리 소계·총계."""
    _require(user, "scm.items", "read")
    from erp.scm.valuation_service import ValuationService
    return await ValuationService().valuation(session)


@router.get("/shortage")
async def inventory_shortage(user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """안전재고 미달 품목 — 현재고 < 안전재고."""
    _require(user, "scm.items", "read")
    from erp.scm.valuation_service import ValuationService
    return await ValuationService().shortage(session)


class ItemNrvBody(BaseModel):
    nrv_unit: float | None = None
    cost_method: str | None = None


@router.patch("/items/{item_id}/nrv")
async def set_item_nrv(item_id: int, body: ItemNrvBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """품목 순실현가능가치(NRV)·원가흐름 설정 (저가법 K-IFRS 1002)."""
    _require(user, "scm.items", "write")
    item = await session.get(Item, item_id)
    if item is None:
        raise ValidationError("품목 없음")
    if body.nrv_unit is not None:
        item.nrv_unit = body.nrv_unit
    if body.cost_method in ("FIFO", "WAVG"):
        item.cost_method = body.cost_method
    return {"item_id": item_id, "nrv_unit": float(item.nrv_unit) if item.nrv_unit is not None else None,
            "cost_method": item.cost_method}


@router.get("/cogs")
async def inventory_cogs(since: date, until: date,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """매출원가(출고원가, K-IFRS 1002) — 기간 출고원가(cost_method 적용)."""
    _require(user, "scm.items", "read")
    from erp.scm.valuation_service import ValuationService
    return await ValuationService().cogs(session, since=since, until=until)


@router.get("/nrv/preview")
async def inventory_nrv_preview(yyyymm: str, user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """재고 저가법 미리보기(K-IFRS 1002) — 품목별 장부가 vs NRV·평가손실·충당차액."""
    require_scope(user, "finance.periods", "read")
    from erp.scm.valuation_service import ValuationService
    return await ValuationService().nrv_preview(session, yyyymm=yyyymm)


@router.post("/nrv/post")
async def inventory_nrv_post(yyyymm: str, user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """재고 저가법 평가손실 충당 결산분개 전기(멱등)."""
    require_scope(user, "finance.periods", "write")
    from erp.scm.valuation_service import ValuationService
    res = await ValuationService().nrv_post(session, yyyymm=yyyymm, actor=user.user_id)
    if res.get("posted"):
        await _audit.record_audit(session, actor=user.user_id, action="INVENTORY_NRV_POST",
                                  entity="finance.periods", target_id=str(res.get("voucher_id")),
                                  after={"yyyymm": yyyymm, "delta": res["delta"]})
    return res


# ── 일일 출입증(알바) — /erp/ws 별도 링크 입장 ──────────────────────
class PassBody(BaseModel):
    label: str | None = None


class PassLoginBody(BaseModel):
    pin: str


def _ws_pass():
    from erp.common.workstation_pass_service import WorkstationPassService
    return WorkstationPassService()


_WS_LOGIN_MAX_FAILS = 10        # per-IP 실패 임계
_WS_LOGIN_WINDOW = 600          # 윈도우(초) — 10분
_WS_LOGIN_GLOBAL_MAX = 60       # 전역 실패 임계(XFF 무관 backstop) — 여러 IP 분산 시도 상한


def _client_ip(request: Request) -> str:
    """스로틀 키용 클라이언트 IP — 신뢰 프록시(nginx `$proxy_add_x_forwarded_for`)가 실제 IP를 XFF
    **오른쪽**에 덧붙이므로 오른쪽 값을 사용. 왼쪽(클라이언트 제공) 값은 스푸핑 가능해 매 요청 변조로
    per-IP 스로틀을 우회할 수 있음(적대검토 MED). 프록시 없으면 request.client.host.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip() or (request.client.host if request.client else "unknown")
    return request.client.host if request.client else "unknown"


async def _ws_login_throttle(ip: str, *, record_fail: bool = False,
                             clear: bool = False) -> None:
    """공개 PIN 입장 무차별 대입 방어 — per-IP + 전역(XFF 무관) 실패 카운트(Redis). redis 불가 시 생략."""
    from erp.common.redis_client import get_redis
    key = f"ws:pinfail:{ip}"
    gkey = "ws:pinfail:global"
    try:
        r = get_redis()
        if clear:
            await r.delete(key)            # 성공 시 per-IP만 리셋(전역은 창 만료로 자연 감쇠)
            return
        if record_fail:
            for k in (key, gkey):
                n = await r.incr(k)
                if n == 1:
                    await r.expire(k, _WS_LOGIN_WINDOW)
            return
        cur = await r.get(key)             # 시도 전 체크(per-IP)
        gcur = await r.get(gkey)           # 전역 backstop — 스푸핑된 XFF 분산 시도 상한
        if (cur is not None and int(cur) >= _WS_LOGIN_MAX_FAILS) or \
           (gcur is not None and int(gcur) >= _WS_LOGIN_GLOBAL_MAX):
            raise ForbiddenError("입장 시도가 너무 많습니다 — 잠시 후 다시 시도하세요")
    except ForbiddenError:
        raise
    except Exception:  # noqa: BLE001 — redis 장애 시 가용성 우선(throttle 생략)
        pass


@router.post("/workstation/passes", status_code=201)
async def create_ws_pass(body: PassBody, user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """일일 출입증 발급(관리자/부서장) — PIN 1회 반환. 당일 23:59 만료."""
    _require(user, "scm.items", "write")
    wp, pin = await _ws_pass().create(session, actor=user.user_id, label=body.label)
    await _audit.record_audit(session, actor=user.user_id, action="WS_PASS_CREATE",
                              entity="common.workstation_passes", target_id=str(wp.id),
                              after={"label": body.label})
    return {"id": wp.id, "pin": pin, "valid_until": kst_iso(wp.valid_until),
            "path": "/erp/ws"}


@router.get("/workstation/passes")
async def list_ws_passes(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """유효 출입증 목록(관리자/부서장)."""
    _require(user, "scm.items", "write")
    return {"items": await _ws_pass().list_active(session)}


@router.post("/workstation/passes/{pass_id}/revoke")
async def revoke_ws_pass(pass_id: int, user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """출입증 폐기(관리자/부서장)."""
    _require(user, "scm.items", "write")
    await _ws_pass().revoke(session, pass_id=pass_id)
    await _audit.record_audit(session, actor=user.user_id, action="WS_PASS_REVOKE",
                              entity="common.workstation_passes", target_id=str(pass_id))
    # 2026-10-09 시나리오 S-D L4: 응답 전에 커밋 — 종전엔 200을 먼저 돌려준 뒤 의존성 종료 시점에
    # 커밋돼, 폐기 직후 요청(4회 중 2회)에서 폐기된 출입증이 아직 통했다. 커밋 후 DB 접근 없음.
    await session.commit()
    return {"id": pass_id, "revoked": True}


@router.post("/workstation/passes/login")
async def ws_pass_login(body: PassLoginBody, request: Request, response: Response,
                        session: AsyncSession = Depends(get_session)):
    """일일 PIN 입장(공개) — 검증 시 WORKSTATION 제한 세션 쿠키 발급(당일 만료).

    무차별 대입 방어: per-IP 실패 카운터가 임계 초과면 차단, 실패 시 증가·성공 시 리셋.
    """
    import secrets as _secrets

    from erp.config import get_settings
    from erp.errors import TokenError
    ip = _client_ip(request)
    await _ws_login_throttle(ip)            # 임계 초과 시 차단
    try:
        out = await _ws_pass().login(session, pin=body.pin)
    except TokenError:
        await _ws_login_throttle(ip, record_fail=True)
        raise
    await _ws_login_throttle(ip, clear=True)
    s = get_settings()
    secure = s.env == "prod"
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    path = f"{prefix}/" or "/"
    response.set_cookie("access_token", out["access_token"], httponly=True, secure=secure,
                        samesite="lax", max_age=86400, path=path)
    response.set_cookie("csrf_token", _secrets.token_hex(16), httponly=False, secure=secure,
                        samesite="lax", max_age=86400, path=path)
    return {"valid_until": out["valid_until"]}


@router.get("/expiring")
async def inventory_expiring(days: int = 90, user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """유효기간 임박 로트 (FR-46) — 워크스테이션 작업 목록·출력용."""
    _require(user, "scm.items", "read")
    lots = await _stock.expiring_lots(session, within_days=days)
    name_map = dict((await session.execute(
        select(Item.id, Item.name).where(Item.id.in_([lot.item_id for lot in lots]))
    )).all()) if lots else {}
    return {"count": len(lots), "items": [
        {"item_id": lot.item_id, "name": name_map.get(lot.item_id, ""), "lot_no": lot.lot_no,
         "expiry_on": lot.expiry_on, "remaining": float(lot.remaining_qty)} for lot in lots]}


# ── 재고 실사(stock-taking) 조정 (FR-46 확장) ────────────────────
class StockTakeBody(BaseModel):
    item_id: int
    physical_qty: float
    taken_on: date
    note: str = ""


@router.post("/stock-takes", status_code=201)
async def record_stocktake(body: StockTakeBody,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """실사 기록 — 장부재고·차이 산정(DRAFT)."""
    _require(user, "scm.items", "write")
    from erp.scm.stocktake_service import StockTakeService
    st = await StockTakeService().record(
        session, item_id=body.item_id, physical_qty=body.physical_qty,
        taken_on=body.taken_on, note=body.note, user_id=user.user_id)
    await session.commit()
    return {"id": st.id, "book_qty": float(st.book_qty), "diff_qty": float(st.diff_qty),
            "diff_value": float(st.diff_value), "status": st.status}


@router.post("/stock-takes/{stocktake_id}/apply")
async def apply_stocktake(stocktake_id: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """실사 반영 — 로트 조정 + 감모손실/조정이익 전표."""
    _require(user, "scm.items", "write")
    from erp.scm.stocktake_service import StockTakeService
    out = await StockTakeService().apply(session, stocktake_id=stocktake_id, user_id=user.user_id)
    await session.commit()
    return out


@router.get("/stock-takes")
async def list_stocktakes(status: str | None = None,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """실사 내역."""
    _require(user, "scm.items", "read")
    from erp.scm.stocktake_service import StockTakeService
    return {"items": await StockTakeService().list_takes(session, status=status)}


@router.get("/turnover")
async def inventory_turnover(since: date, until: date,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """재고회전율 — 기간 출고량/현재고·회전일수·장기재고(dead stock)."""
    _require(user, "scm.items", "read")
    from erp.scm.valuation_service import ValuationService
    return await ValuationService().turnover(session, since=since, until=until)


# ── 위수탁 계약 (전체검토 공백 해소) ──────────────────────
class ContractBody(BaseModel):
    vendor_name: str
    category: str          # MEAL|LAUNDRY|WASTE|SECURITY|CLEANING|FACILITY|IT|INSURANCE|OTHER
    title: str
    monthly_amount: float  # MONTHLY=월정액·ONE_TIME=총액·INSURANCE=보험료(주기)
    start_on: date
    end_on: date
    supplier_id: int | None = None
    auto_renew: bool = False
    contract_type: Literal["OUTSOURCING", "PURCHASE", "SERVICE", "LEASE",
                           "INSURANCE", "OTHER"] = "OUTSOURCING"
    billing_type: Literal["MONTHLY", "ONE_TIME"] = "MONTHLY"
    require_approval: bool = False
    note: str | None = None
    # 보험계약 전용(contract_type=INSURANCE) — 증권번호·보장한도·납입주기(마이그 0220)
    policy_no: str | None = None
    coverage_limit: float | None = None
    premium_cycle: str | None = None   # ANNUAL|SEMIANNUAL|QUARTERLY|MONTHLY


@router.post("/contracts", status_code=201)
async def register_contract(body: ContractBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """계약 등록 — 위수탁(급식·세탁·폐기물·보안) + 일반 구매·용역·임대(contract_type)·월정액/일회성."""
    from erp.scm.contract_service import OutsourcingContractService
    _require(user, "scm.po", "write")
    c = await OutsourcingContractService().register(session, actor=user.user_id,
                                                    **body.model_dump())
    return {"id": c.id}


@router.get("/contracts")
async def list_contracts(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """계약 목록 — D-day·만기임박(60일)·활성 월비용 합계/구분별."""
    from erp.scm.contract_service import OutsourcingContractService
    _require(user, "scm.po", "read")
    return await OutsourcingContractService().list(session)


@router.post("/contracts/{contract_id}/renew")
async def renew_contract(contract_id: int, new_end_on: date,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    from erp.scm.contract_service import OutsourcingContractService
    _require(user, "scm.po", "write")
    c = await OutsourcingContractService().renew(session, actor=user.user_id,
                                                 contract_id=contract_id,
                                                 new_end_on=new_end_on)
    return {"id": c.id, "end_on": c.end_on, "status": c.status}


class ContractTerminateBody(BaseModel):
    reason: str = ""


@router.post("/contracts/{contract_id}/terminate")
async def terminate_contract(contract_id: int, body: ContractTerminateBody | None = None,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    from erp.scm.contract_service import OutsourcingContractService
    _require(user, "scm.po", "write")
    c = await OutsourcingContractService().terminate(
        session, actor=user.user_id, contract_id=contract_id,
        reason=(body.reason if body else ""))
    return {"id": c.id, "status": c.status}


@router.post("/contracts/{contract_id}/submit")
async def submit_contract(contract_id: int, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """결재 상신 — DRAFT→PENDING_APPROVAL."""
    from erp.scm.contract_service import OutsourcingContractService
    _require(user, "scm.po", "write")
    c = await OutsourcingContractService().submit_for_approval(
        session, actor=user.user_id, contract_id=contract_id)
    return {"id": c.id, "status": c.status}


@router.post("/contracts/{contract_id}/approve")
async def approve_contract(contract_id: int, override: bool = False,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """계약 승인 — 직무분리(작성자≠승인자). 결재선 룰 있으면 다단계.

    override=true는 최고경영자 전권(DIRECTOR 전용·엔진 재검증). PENDING_APPROVAL→APPROVED.
    """
    from erp.common.approval_engine import require_scope_or_step_approver
    from erp.scm.contract_service import OutsourcingContractService
    # 2026-10-09 시나리오 S-D M2: 결재선 단계 적격자는 scm.po write 없이도 승인(전권 포함)
    await require_scope_or_step_approver(session, user, "scm.po", "write",
                                         doc_type="OUTSOURCING_CONTRACT", doc_id=contract_id,
                                         override=override)
    c = await OutsourcingContractService().approve(
        session, actor=user.user_id, contract_id=contract_id, override=override)
    return {"id": c.id, "status": c.status,
            "pending_next_step": c.status == "PENDING_APPROVAL"}


@router.post("/contracts/{contract_id}/reject")
async def reject_contract(contract_id: int, body: ContractTerminateBody | None = None,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    from erp.common.approval_engine import require_scope_or_step_approver
    from erp.scm.contract_service import OutsourcingContractService
    await require_scope_or_step_approver(session, user, "scm.po", "write",
                                         doc_type="OUTSOURCING_CONTRACT", doc_id=contract_id)
    c = await OutsourcingContractService().reject(
        session, actor=user.user_id, contract_id=contract_id,
        reason=(body.reason if body else ""))
    return {"id": c.id, "status": c.status}


@router.post("/contracts/{contract_id}/activate")
async def activate_contract(contract_id: int, user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """발효 — APPROVED/DRAFT→ACTIVE(발효일 도래 검증)."""
    from erp.scm.contract_service import OutsourcingContractService
    _require(user, "scm.po", "write")
    c = await OutsourcingContractService().activate(
        session, actor=user.user_id, contract_id=contract_id)
    return {"id": c.id, "status": c.status}


class ContractAmendBody(BaseModel):
    new_monthly: float
    effective_on: date
    reason: str = ""


@router.post("/contracts/{contract_id}/amend-amount")
async def amend_contract_amount(contract_id: int, body: ContractAmendBody,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """월 위탁료 변경 — 변경이력 기록 후 단가 갱신(이후 accrue는 변경액 적용)."""
    from erp.scm.contract_service import OutsourcingContractService
    _require(user, "scm.po", "write")
    c = await OutsourcingContractService().amend_amount(
        session, actor=user.user_id, contract_id=contract_id, new_monthly=body.new_monthly,
        effective_on=body.effective_on, reason=body.reason)
    return {"id": c.id, "monthly_amount": float(c.monthly_amount)}


@router.post("/contracts/{contract_id}/accrue/{yyyymm}")
async def accrue_contract_month(contract_id: int, yyyymm: str,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """월 위탁료 미지급 발생(멱등) — 지급·세금계산서 대사 흐름 연결."""
    from erp.scm.contract_service import OutsourcingContractService
    _require(user, "scm.po", "write")
    return await OutsourcingContractService().accrue_month(
        session, actor=user.user_id, contract_id=contract_id, yyyymm=yyyymm)


@router.get("/contracts/{contract_id}/pdf")
async def contract_pdf(contract_id: int, user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """계약서(안) PDF 자동생성 — 계약 정보 + 변경이력. 정본은 e-sign 별도."""
    from fastapi.responses import Response

    from erp.common.org_service import org_display_name
    from erp.scm.contract_service import OutsourcingContractService
    _require(user, "scm.po", "read")
    org = await org_display_name(session) or "본 의료기관"   # 기관 설정(DB>env) 폴백
    pdf = await OutsourcingContractService().render_pdf(
        session, contract_id=contract_id, org_name=org)
    await _audit.record_access(session, actor=user.user_id, action="PRINT",
                               entity="scm.outsourcing_contracts", target_id=str(contract_id),
                               route=f"/contracts/{contract_id}/pdf")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="contract-{contract_id}.pdf"'})


# ── 의약품 회수·의료폐기물 대장 (규제·안전 logging) ──────────────
class DrugRecallBody(BaseModel):
    item_id: int
    reason: str
    recall_on: date
    lot_no: str | None = None
    severity: str = "VOLUNTARY"


@router.post("/recalls", status_code=201)
async def register_recall(body: DrugRecallBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """의약품 회수 등록 — 보유 영향 로트 식별(격리 대상)."""
    _require(user, "scm.items", "write")
    return await _recall.register(session, item_id=body.item_id, reason=body.reason,
                                  recall_on=body.recall_on, lot_no=body.lot_no,
                                  severity=body.severity, actor=user.user_id)


@router.get("/recalls")
async def list_recalls(status: str | None = None,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """의약품 회수 대장 — 최신순·status 필터."""
    _require(user, "scm.items", "read")
    return {"items": await _recall.list_recalls(session, status=status)}


@router.post("/recalls/{recall_id}/close")
async def close_recall(recall_id: int,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """회수 종결 — 격리·처리 완료."""
    _require(user, "scm.items", "write")
    rec = await _recall.close(session, recall_id=recall_id)
    return {"id": rec.id, "status": rec.status}


class WasteBody(BaseModel):
    waste_type: str
    weight_kg: float
    stored_on: date


@router.post("/waste-logs", status_code=201)
async def record_waste(body: WasteBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """의료폐기물 배출 기록."""
    _require(user, "scm.items", "write")
    w = await _waste.record(session, waste_type=body.waste_type, weight_kg=body.weight_kg,
                            stored_on=body.stored_on, actor=user.user_id)
    return {"id": w.id, "status": w.status}


@router.get("/waste-logs")
async def list_waste_logs(status: str | None = None,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """의료폐기물 배출대장 — 최신순·status 필터."""
    _require(user, "scm.items", "read")
    return {"items": await _waste.list_logs(session, status=status)}


class WasteHandoverBody(BaseModel):
    handed_over_on: date
    handler: str
    manifest_no: str | None = None


@router.patch("/waste-logs/{log_id}/handover")
async def handover_waste(log_id: int, body: WasteHandoverBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """폐기물 처리업체 인계 — 올바로 인계서 번호 기록."""
    _require(user, "scm.items", "write")
    w = await _waste.hand_over(session, log_id=log_id, handed_over_on=body.handed_over_on,
                               handler=body.handler, manifest_no=body.manifest_no)
    return {"id": w.id, "status": w.status}


# ── 소싱(RFQ→견적→단가계약) ──────────────────────────────
class RfqLineBody(BaseModel):
    item_id: int
    qty: float
    spec: str | None = None


class RfqBody(BaseModel):
    title: str
    lines: list[RfqLineBody]
    category: str | None = None
    due_on: date | None = None
    note: str | None = None


class QuoteLineBody(BaseModel):
    item_id: int
    unit_price: float
    lead_time_days: int | None = None


class QuoteBody(BaseModel):
    supplier_id: int
    lines: list[QuoteLineBody]
    submitted_on: date | None = None
    note: str | None = None


class AwardBody(BaseModel):
    quote_id: int
    valid_from: date
    valid_to: date
    title: str | None = None


@router.post("/rfqs", status_code=201)
async def create_rfq(body: RfqBody, user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """견적요청(RFQ) 등록 — 소싱 상류."""
    from erp.scm.rfq_service import RfqService
    _require(user, "scm.po", "write")
    rfq = await RfqService().create_rfq(
        session, actor=user.user_id, title=body.title,
        lines=[ln.model_dump() for ln in body.lines], category=body.category,
        due_on=body.due_on, note=body.note)
    return {"id": rfq.id, "status": rfq.status}


@router.get("/rfqs")
async def list_rfqs(status: str | None = None,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """RFQ 목록 — 최신순·라인/견적 건수."""
    from erp.scm.rfq_service import RfqService
    _require(user, "scm.po", "read")
    return {"items": await RfqService().list_rfqs(session, status=status)}


@router.get("/rfqs/{rfq_id}")
async def rfq_detail(rfq_id: int, user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """RFQ 상세 — 헤더·요청 라인·접수 견적."""
    from erp.scm.rfq_service import RfqService
    _require(user, "scm.po", "read")
    return await RfqService().detail(session, rfq_id=rfq_id)


@router.post("/rfqs/{rfq_id}/open")
async def open_rfq(rfq_id: int, user: CurrentUser = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    from erp.scm.rfq_service import RfqService
    _require(user, "scm.po", "write")
    rfq = await RfqService().open_rfq(session, actor=user.user_id, rfq_id=rfq_id)
    return {"id": rfq.id, "status": rfq.status}


@router.post("/rfqs/{rfq_id}/quotes", status_code=201)
async def submit_quote(rfq_id: int, body: QuoteBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """벤더 견적 접수(내부 구매담당 입력)."""
    from erp.scm.rfq_service import RfqService
    _require(user, "scm.po", "write")
    q = await RfqService().submit_quote(
        session, actor=user.user_id, rfq_id=rfq_id, supplier_id=body.supplier_id,
        lines=[ln.model_dump() for ln in body.lines], submitted_on=body.submitted_on,
        note=body.note)
    return {"id": q.id, "total": float(q.total), "status": q.status}


@router.get("/rfqs/{rfq_id}/compare")
async def compare_quotes(rfq_id: int, user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """견적 비교 — 총액 최저가 순위."""
    from erp.scm.rfq_service import RfqService
    _require(user, "scm.po", "read")
    return {"items": await RfqService().compare(session, rfq_id=rfq_id)}


@router.post("/rfqs/{rfq_id}/award")
async def award_rfq(rfq_id: int, body: AwardBody,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """낙찰 — 견적 선정 → 단가계약 생성(나머지 견적 반려)."""
    from erp.scm.rfq_service import RfqService
    _require(user, "scm.po", "write")
    pa = await RfqService().award(session, actor=user.user_id, rfq_id=rfq_id,
                                  quote_id=body.quote_id, valid_from=body.valid_from,
                                  valid_to=body.valid_to, title=body.title)
    return {"price_agreement_id": pa.id, "supplier_id": pa.supplier_id}


@router.get("/price-agreements")
async def list_price_agreements(user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """유효 단가계약 목록."""
    from erp.scm.rfq_service import PriceAgreementService
    _require(user, "scm.po", "read")
    return {"items": await PriceAgreementService().list_active(session)}


# ── 구매요청(PR)·자재청구 — 요청→승인→실행 (마이그 0217) ─────────
_preq = None
_mreq = None


def _preq_service():
    global _preq
    if _preq is None:
        from erp.scm.requisition_service import PurchaseRequisitionService
        _preq = PurchaseRequisitionService()
    return _preq


def _mreq_service():
    global _mreq
    if _mreq is None:
        from erp.scm.requisition_service import MaterialRequestService
        _mreq = MaterialRequestService()
    return _mreq


class ReqLineBody(BaseModel):
    item_id: int
    qty: float
    est_price: float | None = None
    note: str | None = None


class RequisitionBody(BaseModel):
    lines: list[ReqLineBody]
    reason: str | None = None
    needed_by: date | None = None


@router.post("/requisitions", status_code=201)
async def create_requisition(body: RequisitionBody,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """구매요청(PR) 기안 — 부서 수요. 승인 후 PO 전환."""
    _require(user, "scm.requisition", "write")
    req = await _preq_service().create(
        session, requester=user.user_id, department_id=user.dept_id, reason=body.reason,
        needed_by=body.needed_by, lines=[ln.model_dump() for ln in body.lines])
    await session.commit()
    return {"id": req.id, "req_no": req.req_no, "status": req.status}


@router.get("/requisitions")
async def list_requisitions(status: str | None = None,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    _require(user, "scm.requisition", "read")
    return {"items": await _preq_service().list_requisitions(session, status=status)}


@router.get("/requisitions/{req_id}")
async def get_requisition(req_id: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """구매요청 상세 — 헤더+라인(품목명). FE 상세·발주전환 화면 소스."""
    _require(user, "scm.requisition", "read")
    return await _preq_service().get_detail(session, req_id)


class DecideBody(BaseModel):
    approve: bool
    reason: str = ""


@router.post("/requisitions/{req_id}/decide")
async def decide_requisition(req_id: int, body: DecideBody,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """구매요청 승인/반려 — 직무분리(요청자≠승인자)."""
    from erp.common.policy import ScopeKind
    scope = _require(user, "scm.requisition", "approve")
    r = await _preq_service().decide(session, req_id=req_id, approve=body.approve,
                                     actor=user.user_id, reason=body.reason,
                                     dept_only=(scope.kind == ScopeKind.DEPT),
                                     approver_dept=user.dept_id)
    await session.commit()
    return {"id": r.id, "status": r.status}


@router.post("/requisitions/{req_id}/cancel")
async def cancel_requisition(req_id: int,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    _require(user, "scm.requisition", "write")
    r = await _preq_service().cancel(session, req_id=req_id, requester=user.user_id)
    await session.commit()
    return {"id": r.id, "status": r.status}


class ConvertBody(BaseModel):
    supplier_id: int
    unit_prices: dict[str, float] = {}    # {item_id: price}


@router.post("/requisitions/{req_id}/convert")
async def convert_requisition(req_id: int, body: ConvertBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """승인된 구매요청 → 발주 전환."""
    from erp.common.policy import ScopeKind
    scope = _require(user, "scm.requisition", "approve")
    out = await _preq_service().convert_to_po(
        session, req_id=req_id, supplier_id=body.supplier_id,
        unit_prices=body.unit_prices, actor=user.user_id,
        dept_only=(scope.kind == ScopeKind.DEPT), approver_dept=user.dept_id)
    await session.commit()
    return out


class MaterialReqBody(BaseModel):
    item_id: int
    qty: float
    reason: str | None = None


@router.post("/material-requests", status_code=201)
async def create_material_request(body: MaterialReqBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """자재청구(불출요청) 기안 — 병동/부서. 승인 후 FIFO 출고."""
    _require(user, "scm.requisition", "write")
    req = await _mreq_service().create(
        session, requester=user.user_id, department_id=user.dept_id, item_id=body.item_id,
        qty=body.qty, reason=body.reason)
    await session.commit()
    return {"id": req.id, "req_no": req.req_no, "status": req.status}


@router.get("/material-requests")
async def list_material_requests(status: str | None = None,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    _require(user, "scm.requisition", "read")
    return {"items": await _mreq_service().list_requests(session, status=status)}


@router.post("/material-requests/{req_id}/decide")
async def decide_material_request(req_id: int, body: DecideBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """자재청구 승인/반려 — 직무분리(요청자≠승인자)."""
    from erp.common.policy import ScopeKind
    scope = _require(user, "scm.requisition", "approve")
    r = await _mreq_service().decide(session, req_id=req_id, approve=body.approve,
                                     actor=user.user_id, reason=body.reason,
                                     dept_only=(scope.kind == ScopeKind.DEPT),
                                     approver_dept=user.dept_id)
    await session.commit()
    return {"id": r.id, "status": r.status}


@router.post("/material-requests/{req_id}/cancel")
async def cancel_material_request(req_id: int,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    _require(user, "scm.requisition", "write")
    r = await _mreq_service().cancel(session, req_id=req_id, requester=user.user_id)
    await session.commit()
    return {"id": r.id, "status": r.status}


class MaterialIssueBody(BaseModel):
    moved_on: date | None = None


@router.post("/material-requests/{req_id}/issue")
async def issue_material_request(req_id: int, body: MaterialIssueBody,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """승인된 자재청구 → FIFO 출고(승인 후에만)."""
    from erp.common.policy import ScopeKind
    scope = _require(user, "scm.requisition", "approve")
    out = await _mreq_service().issue(session, req_id=req_id, actor=user.user_id,
                                      moved_on=body.moved_on,
                                      dept_only=(scope.kind == ScopeKind.DEPT),
                                      approver_dept=user.dept_id)
    await session.commit()
    return out


# ── 위탁재고(Consignment) — 소유권 미이전 보관·사용 시점 매입 (마이그 0219) ──
_consign = None


def _consign_service():
    global _consign
    if _consign is None:
        from erp.scm.consignment_service import ConsignmentService
        _consign = ConsignmentService()
    return _consign


class ConsignPlaceBody(BaseModel):
    supplier_id: int
    item_id: int
    lot_no: str
    qty: float
    unit_cost: float
    placed_on: date
    expiry_on: date | None = None
    note: str | None = None


@router.post("/consignment", status_code=201)
async def place_consignment(body: ConsignPlaceBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """위탁재고 적재 — 공급사 소유 상태로 비치(회계 없음·미소유)."""
    _require(user, "scm.po", "write")
    cs = await _consign_service().place(session, **body.model_dump())
    await session.commit()
    return {"id": cs.id, "status": cs.status, "qty_on_hand": float(cs.qty_on_hand)}


@router.get("/consignment")
async def list_consignment(status: str | None = "ACTIVE", supplier_id: int | None = None,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    _require(user, "scm.po", "read")
    return {"items": await _consign_service().list_stock(session, status=status,
                                                         supplier_id=supplier_id)}


class ConsignConsumeBody(BaseModel):
    qty: float
    consumed_on: date
    chart_no: str | None = None
    note: str | None = None
    client_ref: str | None = None   # 멱등키(F5·재전송 디둡)


@router.post("/consignment/{consignment_id}/consume")
async def consume_consignment(consignment_id: int, body: ConsignConsumeBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """위탁재고 사용 — 소유권 이전·매입 인식(DR 5410/CR 2110)·미지급 발생."""
    _require(user, "scm.po", "write")
    con = await _consign_service().consume(
        session, consignment_id=consignment_id, qty=body.qty, consumed_on=body.consumed_on,
        chart_no=body.chart_no, note=body.note, actor=user.user_id, client_ref=body.client_ref)
    await session.commit()
    return {"id": con.id, "amount": float(con.amount), "payable_ref": con.payable_ref}


class ConsignReturnBody(BaseModel):
    qty: float
    returned_on: date
    note: str | None = None


@router.post("/consignment/{consignment_id}/return")
async def return_consignment(consignment_id: int, body: ConsignReturnBody,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """미사용 위탁재고 공급사 반환 — 회계 없음(미소유)."""
    _require(user, "scm.po", "write")
    cs = await _consign_service().return_stock(
        session, consignment_id=consignment_id, qty=body.qty, returned_on=body.returned_on,
        note=body.note, actor=user.user_id)
    await session.commit()
    return {"id": cs.id, "status": cs.status, "qty_on_hand": float(cs.qty_on_hand)}


@router.get("/consignment-consumptions")
async def list_consignment_consumptions(since: date | None = None, until: date | None = None,
                                        user: CurrentUser = Depends(get_current_user),
                                        session: AsyncSession = Depends(get_session)):
    """위탁재고 사용(매입) 내역 — 기간 총액(관리회계·AP 대사).

    chart_no는 조달 스코프에 그대로 노출하지 않는다(Phase0 규율) — 환자 식별 권한
    (`patient.patients read`)이 있는 호출자에게만 원문을 준다. 그 외에는 마스킹.
    """
    _require(user, "scm.po", "read")
    from erp.common.policy import Role, ScopeKind, resolve_scope
    unmask = resolve_scope("patient.patients", "read", role=Role(user.role),
                           dept_id=user.dept_id, user_id=user.user_id).kind != ScopeKind.NONE
    return await _consign_service().list_consumptions(session, since=since, until=until,
                                                      unmask_chart=unmask)
