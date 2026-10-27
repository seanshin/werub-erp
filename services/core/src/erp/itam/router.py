"""ITAM 라우터 — IT 자산 대장·배정·벤더 계약 (M1). RBAC: itam.assets."""
from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.errors import ForbiddenError
from erp.itam.cost_service import CostService
from erp.itam.models import ITAsset, SoftwareService
from erp.itam.service import ITAMService
from erp.itam.software_service import SoftwareServiceService

router = APIRouter(prefix="/itam", tags=["itam"])
_svc = ITAMService()
_sw = SoftwareServiceService()
_cost = CostService()


def _require(user: CurrentUser, action: str):
    return require_scope(user, "itam.assets", action)


async def _self_employee_id(session: AsyncSession, user: CurrentUser) -> int:
    """로그인 사용자 → 연결 직원 id. 직원 계정이 아니면 차단(본인 확인 경로·F-7)."""
    from erp.common.models import User
    eid = (await session.execute(
        select(User.employee_id).where(User.id == user.user_id))).scalar_one_or_none()
    if eid is None:
        raise ForbiddenError("직원 계정이 아닙니다")
    return eid


def _csv_response(text: str, filename: str) -> Response:
    # BOM 부착(Excel 한글 인코딩)·다운로드 헤더
    return Response(content="﻿" + text, media_type="text/csv; charset=utf-8",
                   headers={"Content-Disposition": f"attachment; filename={filename}"})


class AssetBody(BaseModel):
    asset_tag: str
    name: str
    category: str = "LAPTOP"
    serial_no: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    status: str = "IN_STOCK"
    owner_department_id: int | None = None
    location: str | None = None
    acquired_on: date | None = None
    warranty_until: date | None = None
    ip_addr: str | None = None
    hostname: str | None = None
    mac_addr: str | None = None
    handles_phi: bool = False
    security_grade: str | None = None
    disk_encrypted: bool | None = None
    av_installed: bool | None = None
    udi: str | None = None
    mfds_no: str | None = None
    calibration_due: date | None = None
    next_inspection_on: date | None = None
    next_maintenance_on: date | None = None
    return_due_on: date | None = None
    assigned_employee_id: int | None = None      # 사용자(등록 시 배정)
    spec: dict | None = None                     # 장비 디테일(CPU/RAM/디스크)
    finance_asset_id: int | None = None
    contract_id: int | None = None
    note: str | None = None


class AssetPatch(BaseModel):
    name: str | None = None
    category: str | None = None
    status: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    owner_department_id: int | None = None
    location: str | None = None
    warranty_until: date | None = None
    ip_addr: str | None = None
    hostname: str | None = None
    mac_addr: str | None = None
    handles_phi: bool | None = None
    security_grade: str | None = None
    disk_encrypted: bool | None = None
    av_installed: bool | None = None
    udi: str | None = None
    mfds_no: str | None = None
    calibration_due: date | None = None
    next_inspection_on: date | None = None
    next_maintenance_on: date | None = None
    return_due_on: date | None = None
    spec: dict | None = None
    finance_asset_id: int | None = None
    contract_id: int | None = None
    note: str | None = None


class BulkAssetRow(BaseModel):
    asset_tag: str
    name: str | None = None
    category: str | None = None
    serial_no: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    status: str | None = None
    location: str | None = None
    warranty_until: date | None = None
    ip_addr: str | None = None
    hostname: str | None = None
    handles_phi: bool | None = None


class BulkAssetBody(BaseModel):
    rows: list[BulkAssetRow]


class AssignBody(BaseModel):
    employee_id: int
    assigned_on: date | None = None
    note: str | None = None


class ReturnBody(BaseModel):
    on: date | None = None
    condition: str | None = None


class ContractBody(BaseModel):
    vendor: str
    title: str
    kind: str = "PURCHASE"
    signed_on: date | None = None            # 체결일
    start_on: date | None = None             # 발효일
    end_on: date | None = None               # 만기
    next_review_on: date | None = None       # 차기 검토·갱신 시기
    amount: float | None = None
    counterparty: str | None = None          # 상대 담당자
    counterparty_contact: str | None = None  # 상대 담당자 연락처
    owner_employee_id: int | None = None     # 내부 담당자
    department_id: int | None = None         # 담당 부서
    reason: str | None = None                # 사유
    sla_note: str | None = None
    note: str | None = None                  # 비고·주요 사항


class ContractPatch(BaseModel):
    vendor: str | None = None
    title: str | None = None
    kind: str | None = None
    signed_on: date | None = None
    start_on: date | None = None
    end_on: date | None = None
    next_review_on: date | None = None
    amount: float | None = None
    counterparty: str | None = None
    counterparty_contact: str | None = None
    owner_employee_id: int | None = None
    department_id: int | None = None
    reason: str | None = None
    sla_note: str | None = None
    note: str | None = None


def _asset_dict(a: ITAsset) -> dict:
    return {
        "id": a.id, "asset_tag": a.asset_tag, "name": a.name, "category": a.category,
        "serial_no": a.serial_no, "model": a.model, "manufacturer": a.manufacturer,
        "status": a.status, "owner_department_id": a.owner_department_id,
        "assigned_employee_id": a.assigned_employee_id, "location": a.location,
        "acquired_on": a.acquired_on, "warranty_until": a.warranty_until,
        "ip_addr": a.ip_addr, "hostname": a.hostname, "mac_addr": a.mac_addr,
        "handles_phi": a.handles_phi,
        # sticky PHI — 폐기 게이트는 `handles_phi or phi_ever`로 판정하는데(service.py) 이 값을
        # 안 내보내면, 과거 PHI를 취급했다 해제된 장비는 화면에 소거 체크박스가 뜨지 않은 채
        # 폐기가 영구 400이 된다(실동작 불일치).
        "phi_ever": a.phi_ever, "security_grade": a.security_grade,
        "disk_encrypted": a.disk_encrypted, "av_installed": a.av_installed,
        "udi": a.udi, "mfds_no": a.mfds_no, "calibration_due": a.calibration_due,
        "next_inspection_on": a.next_inspection_on, "next_maintenance_on": a.next_maintenance_on,
        "return_due_on": a.return_due_on, "spec": a.spec,
        "acquisition_type": a.acquisition_type,
        "finance_asset_id": a.finance_asset_id, "lease_id": a.lease_id, "contract_id": a.contract_id,
        "last_audited_at": a.last_audited_at, "procurement_po_line_id": a.procurement_po_line_id,
        "data_erased": a.data_erased, "disposed_at": a.disposed_at, "note": a.note,
    }


# ── 자산 ────────────────────────────────────────────────────
@router.get("/employees")
async def employee_picker(q: str | None = None, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """자산 배정용 직원 선택 목록(재직자·id·사번·이름)."""
    _require(user, "read")
    return {"items": await _svc.employee_options(session, q=q)}


@router.post("/assets", status_code=201)
async def create_asset(body: AssetBody, user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    data = body.model_dump()
    emp_id = data.pop("assigned_employee_id", None)
    a = await _svc.create_asset(session, actor=user.user_id, **data)
    if emp_id:                                   # 등록 시 사용자 배정(이력 생성)
        await _svc.assign(session, actor=user.user_id, asset_id=a.id, employee_id=emp_id)
    await session.commit()
    return _asset_dict(a)


@router.get("/assets")
async def list_assets(category: str | None = None, status: str | None = None,
                      department_id: int | None = None, handles_phi: bool | None = None,
                      assigned: bool | None = None, q: str | None = None,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    items = await _svc.list_assets(session, category=category, status=status,
                                   department_id=department_id, handles_phi=handles_phi,
                                   assigned=assigned, q=q)
    counts = await _svc.counts(session)
    names = await _svc.employee_names(session, [a.assigned_employee_id for a in items])
    return {"items": [{**_asset_dict(a),
                       "assigned_employee_name": (names.get(a.assigned_employee_id) or {}).get("name")}
                      for a in items], "counts": counts}


@router.post("/assets/bulk")
async def bulk_upsert(body: BulkAssetBody, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """스프레드시트 일괄 저장 — asset_tag 키 upsert(신규/갱신)·행 격리."""
    _require(user, "write")
    out = await _svc.bulk_upsert(session, actor=user.user_id,
                                 rows=[r.model_dump() for r in body.rows])
    await session.commit()
    return out


@router.get("/assets/suggest-tag")
async def suggest_tag(prefix: str = "IT", user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """QR 자산 태그 자동 생성 제안(등록 폼) — 다음 순번 태그."""
    _require(user, "read")
    return {"tag": await _svc.suggest_tag(session, prefix)}


@router.get("/assets/lookup")
async def lookup_asset(tag: str, user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """QR/바코드 스캔 조회 — 자산 태그로 1건(사용자 이름 병기 — 현장 실사)."""
    _require(user, "read")
    a = await _svc.lookup_by_tag(session, tag)
    if a is None:
        return {"found": False, "asset": None}
    names = await _svc.employee_names(session, [a.assigned_employee_id])
    return {"found": True, "asset": {
        **_asset_dict(a),
        "assigned_employee_name": (names.get(a.assigned_employee_id) or {}).get("name")}}


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: int, user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    a = await _svc.get_asset(session, asset_id)
    asns = await _svc.assignments(session, asset_id)
    mnts = await _svc.maintenance_records(session, asset_id)
    names = await _svc.employee_names(
        session, [a.assigned_employee_id, *[x.employee_id for x in asns]])
    cur = names.get(a.assigned_employee_id) or {}
    return {**_asset_dict(a),
            "assigned_employee_name": cur.get("name"), "assigned_employee_no": cur.get("employee_no"),
            "lease": await _svc.lease_summary(session, asset_id),   # IFRS16 리스 요약(0227)
            "disposal_approval": await _svc.disposal_status(session, asset_id),   # F-3 결재 진행 상태
            "assignments": [
        {"id": x.id, "employee_id": x.employee_id,
         "employee_name": (names.get(x.employee_id) or {}).get("name"),
         "assigned_on": x.assigned_on,
         "returned_on": x.returned_on, "acknowledged_at": x.acknowledged_at,
         "return_condition": x.return_condition} for x in asns],
        "maintenance": [
        {"id": m.id, "kind": m.kind, "on_date": m.on_date, "vendor": m.vendor,
         "cost": float(m.cost) if m.cost is not None else None, "note": m.note} for m in mnts]}


@router.patch("/assets/{asset_id}")
async def patch_asset(asset_id: int, body: AssetPatch,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    a = await _svc.update_asset(session, actor=user.user_id, asset_id=asset_id, **fields)
    await session.commit()
    return _asset_dict(a)


@router.post("/assets/{asset_id}/assign")
async def assign_asset(asset_id: int, body: AssignBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    asn = await _svc.assign(session, actor=user.user_id, asset_id=asset_id,
                            employee_id=body.employee_id, assigned_on=body.assigned_on,
                            note=body.note)
    await session.commit()
    return {"assignment_id": asn.id, "asset_id": asset_id, "employee_id": body.employee_id}


@router.post("/assets/{asset_id}/return")
async def return_asset(asset_id: int, body: ReturnBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    await _svc.return_asset(session, actor=user.user_id, asset_id=asset_id,
                            on=body.on, condition=body.condition)
    await session.commit()
    return {"ok": True}


class MaintenanceBody(BaseModel):
    kind: str = "REPAIR"
    on_date: date
    vendor: str | None = None
    cost: float | None = None
    note: str | None = None


class DisposeBody(BaseModel):
    data_erased: bool = False
    on: date | None = None
    note: str | None = None
    # 폐기 결재 금액 기준은 서버(연결 고정자산 장부가)에서 도출 — 클라이언트 입력 미신뢰(재검토 HIGH)


class DisposalApprovalBody(BaseModel):
    approve: bool = True
    override: bool = False       # 최고경영자 전권(DIRECTOR)
    note: str | None = None


class AuditStampBody(BaseModel):
    location: str | None = None


@router.post("/assets/{asset_id}/maintenance", status_code=201)
async def add_maintenance(asset_id: int, body: MaintenanceBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    m = await _svc.add_maintenance(session, actor=user.user_id, asset_id=asset_id,
                                   **body.model_dump())
    await session.commit()
    return {"id": m.id, "kind": m.kind, "on_date": m.on_date}


class LeaseAttachBody(BaseModel):
    start_on: date
    term_months: int
    monthly_payment: float
    annual_rate: float = 0
    lessor: str = ""
    exempt: bool = False           # 단기(≤12M)·소액 리스 면제(IFRS16)
    lease_no: str | None = None    # 미지정 시 ITL-{asset_tag}


@router.post("/assets/{asset_id}/lease", status_code=201)
async def attach_lease(asset_id: int, body: LeaseAttachBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """IT자산 리스 취득 — IFRS16 리스계약(사용권자산·리스부채) 개시·연결(0227).

    구매(finance_asset_id) 취득 자산에는 불가(상호 배타). 월 상각/이자는 finance 리스 월마감 경유."""
    _require(user, "write")
    out = await _svc.attach_lease(session, actor=user.user_id, asset_id=asset_id,
                                  **body.model_dump())
    await session.commit()
    return out


@router.post("/assets/{asset_id}/dispose")
async def dispose_asset(asset_id: int, body: DisposeBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """폐기 — PHI 취급 장비는 데이터 소거 확인(data_erased) 필수. 결재 규칙 있으면 승인 후 폐기(F-3)."""
    _require(user, "write")
    a = await _svc.dispose(session, actor=user.user_id, asset_id=asset_id,
                           data_erased=body.data_erased, on=body.on, note=body.note)
    await session.commit()
    return _asset_dict(a)


@router.post("/assets/{asset_id}/dispose-approval")
async def dispose_approval(asset_id: int, body: DisposalApprovalBody,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """폐기 결재 승인/반려(F-3) — 결재 권한(common.approvals)·엔진 직무분리·단계 자격 검증."""
    require_scope(user, "common.approvals", "write")
    out = await _svc.disposal_approval(session, actor=user.user_id, asset_id=asset_id,
                                       approve=body.approve, override=body.override, note=body.note)
    await session.commit()
    return out


# ── 실사 세션(F-9) ───────────────────────────────────────────
@router.post("/assets/{asset_id}/audit-stamp")
async def audit_stamp(asset_id: int, body: AuditStampBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """QR 현장 실사 확인 — last_audited_at 스탬프(+위치 갱신)."""
    _require(user, "write")
    a = await _svc.audit_stamp(session, actor=user.user_id, asset_id=asset_id,
                               location=body.location)
    await session.commit()
    return {"id": a.id, "last_audited_at": a.last_audited_at, "location": a.location}


@router.get("/audit-report")
async def audit_report(days: int = 180, user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """미실사 자산 리포트 — 기준일수 내 실사 기록 없는 운영 자산."""
    _require(user, "read")
    return await _svc.audit_report(session, days=days)


@router.post("/assignments/{assignment_id}/acknowledge")
async def acknowledge(assignment_id: int, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    await _svc.acknowledge(session, actor=user.user_id, assignment_id=assignment_id)
    await session.commit()
    return {"ok": True}


# ── 본인 배정 자산·수령 확인(F-7·인증만·관리자 권한 불요) ─────
@router.get("/my/assignments")
async def my_assignments(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """로그인 직원 본인에게 현재 배정된 자산(수령 확인 대기 포함)."""
    eid = await _self_employee_id(session, user)
    return {"items": await _svc.my_assignments(session, eid)}


@router.post("/my/assignments/{assignment_id}/acknowledge")
async def acknowledge_self(assignment_id: int, user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """본인 수령 확인(chain-of-custody) — 배정 당사자 직원만. 대리 확인 차단."""
    eid = await _self_employee_id(session, user)
    await _svc.acknowledge(session, actor=user.user_id, assignment_id=assignment_id,
                           employee_id=eid)
    await session.commit()
    return {"ok": True}


# ── 변경 이력(감사 기반·F-8) ─────────────────────────────────
@router.get("/assets/{asset_id}/history")
async def asset_history(asset_id: int, user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    return {"items": await _svc.change_history(session, entity="itam.it_assets",
                                               target_id=asset_id)}


@router.get("/services/{service_id}/history")
async def service_history(service_id: int, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """서비스 갱신 이력 — 만기·단가 변경 추적(F-8)."""
    _require(user, "read")
    return {"items": await _svc.change_history(session, entity="itam.software_services",
                                               target_id=service_id)}


# ── 내보내기(CSV·감사 제출용·F-10) ───────────────────────────
@router.get("/assets/export.csv")
async def export_assets(category: str | None = None, status: str | None = None,
                        department_id: int | None = None, handles_phi: bool | None = None,
                        assigned: bool | None = None, q: str | None = None,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    text = await _svc.assets_csv(session, category=category, status=status,
                                 department_id=department_id, handles_phi=handles_phi,
                                 assigned=assigned, q=q)
    return _csv_response(text, "it-assets.csv")


@router.get("/cost-ledger.csv")
async def export_cost_ledger(period: str | None = None,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    text = await _cost.ledger_csv(session, period=period)
    return _csv_response(text, f"it-costs-{period or 'all'}.csv")


# ── 퇴사 회수(offboarding) (M4) ──────────────────────────────
@router.get("/offboarding")
async def offboarding(user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """퇴사자 중 IT 자산·SW 좌석 미회수 목록."""
    _require(user, "read")
    return {"items": await _svc.offboarding_pending(session)}


@router.get("/employees/{employee_id}/holdings")
async def holdings(employee_id: int, user: CurrentUser = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    """직원 현재 보유 IT 자산 + SW 좌석(회수 대상)."""
    _require(user, "read")
    return await _svc.employee_holdings(session, employee_id)


# ── 조달 PO 연계(R4) — 입고된 EQUIPMENT → IT 자산 등록 ────────
class ProcurementRegisterBody(BaseModel):
    asset_tag: str | None = None       # 미지정 시 자동 발번
    category: str = "PERIPHERAL"
    serial_no: str | None = None
    name: str | None = None            # 미지정 시 품목명
    handles_phi: bool = False
    model: str | None = None
    location: str | None = None


@router.get("/procurement/candidates")
async def procurement_candidates(user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """입고된 구매발주(EQUIPMENT) 중 IT 자산 미등록 후보."""
    _require(user, "read")
    return {"items": await _svc.procurement_candidates(session)}


@router.post("/procurement/{po_line_id}/register", status_code=201)
async def register_from_po(po_line_id: int, body: ProcurementRegisterBody,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """입고 PO 라인을 IT 자산으로 등록(품목·공급사·도입일·부서 프리필)."""
    _require(user, "write")
    a = await _svc.register_from_po(session, actor=user.user_id, po_line_id=po_line_id,
                                    **body.model_dump())
    await session.commit()
    return _asset_dict(a)


# ── 퇴사 회수 기록(M4 확대·#4) ───────────────────────────────
class OffboardingBody(BaseModel):
    employee_id: int
    reviewer_id: int | None = None
    handover_to_id: int | None = None
    note: str | None = None


class OffboardingPatch(BaseModel):
    reviewer_id: int | None = None
    handover_to_id: int | None = None
    note: str | None = None


async def _off_dict(session, o) -> dict:
    names = await _svc.employee_names(
        session, [o.employee_id, o.reviewer_id, o.handover_to_id])
    return {"id": o.id, "employee_id": o.employee_id,
            "employee_name": (names.get(o.employee_id) or {}).get("name"),
            "reviewer_id": o.reviewer_id,
            "reviewer_name": (names.get(o.reviewer_id) or {}).get("name"),
            "handover_to_id": o.handover_to_id,
            "handover_to_name": (names.get(o.handover_to_id) or {}).get("name"),
            "status": o.status, "note": o.note, "completed_on": o.completed_on}


@router.get("/offboardings/{employee_id}")
async def get_offboarding(employee_id: int, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """직원 퇴사 회수 기록(없으면 record=null)."""
    _require(user, "read")
    o = await _svc.offboarding_get(session, employee_id)
    return {"record": await _off_dict(session, o) if o else None}


@router.post("/offboardings", status_code=201)
async def create_offboarding(body: OffboardingBody, user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    o = await _svc.offboarding_create(session, actor=user.user_id, **body.model_dump())
    await session.commit()
    return await _off_dict(session, o)


@router.patch("/offboardings/{offboarding_id}")
async def patch_offboarding(offboarding_id: int, body: OffboardingPatch,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    o = await _svc.offboarding_update(session, actor=user.user_id, offboarding_id=offboarding_id,
                                      **fields)
    await session.commit()
    return await _off_dict(session, o)


@router.post("/offboardings/{offboarding_id}/complete")
async def complete_offboarding(offboarding_id: int, user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """회수 완료 — 미회수 자산·좌석 남으면 차단."""
    _require(user, "write")
    o = await _svc.offboarding_complete(session, actor=user.user_id, offboarding_id=offboarding_id)
    await session.commit()
    return await _off_dict(session, o)


# ── 벤더 계약 ────────────────────────────────────────────────
def _contract_dict(c, names: dict, depts: dict) -> dict:
    return {
        "id": c.id, "vendor": c.vendor, "title": c.title, "kind": c.kind,
        "signed_on": c.signed_on, "start_on": c.start_on, "end_on": c.end_on,
        "next_review_on": c.next_review_on,
        "amount": float(c.amount) if c.amount is not None else None,
        "counterparty": c.counterparty, "counterparty_contact": c.counterparty_contact,
        "owner_employee_id": c.owner_employee_id,
        "owner_employee_name": (names.get(c.owner_employee_id) or {}).get("name"),
        "department_id": c.department_id, "department_name": depts.get(c.department_id),
        "reason": c.reason, "sla_note": c.sla_note, "note": c.note,
    }


async def _contract_labels(session, items) -> tuple[dict, dict]:
    """계약 owner 직원명·부서명 해석."""
    from erp.common.models import Department
    names = await _svc.employee_names(session, [c.owner_employee_id for c in items])
    dept_ids = [c.department_id for c in items if c.department_id]
    depts = {}
    if dept_ids:
        rows = (await session.execute(
            select(Department.id, Department.name).where(Department.id.in_(dept_ids)))).all()
        depts = {i: n for i, n in rows}
    return names, depts


@router.post("/contracts", status_code=201)
async def create_contract(body: ContractBody, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    c = await _svc.create_contract(session, actor=user.user_id, **body.model_dump())
    await session.commit()
    return {"id": c.id, "vendor": c.vendor, "title": c.title, "kind": c.kind, "end_on": c.end_on}


@router.get("/contracts")
async def list_contracts(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    items = await _svc.list_contracts(session)
    names, depts = await _contract_labels(session, items)
    return {"items": [_contract_dict(c, names, depts) for c in items]}


@router.patch("/contracts/{contract_id}")
async def patch_contract(contract_id: int, body: ContractPatch,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    c = await _svc.update_contract(session, actor=user.user_id, contract_id=contract_id, **fields)
    await session.commit()
    names, depts = await _contract_labels(session, [c])
    return _contract_dict(c, names, depts)


# ── 소프트웨어·디지털 서비스 (M2) ────────────────────────────
class ServiceBody(BaseModel):
    name: str
    service_type: str = "SAAS"
    billing_model: str = "SEAT_RECURRING"
    billing_cycle: str | None = None
    vendor: str | None = None
    edition: str | None = None
    seats: int | None = None
    license_metric: str | None = None
    alloc_basis: str | None = None
    unit_cost: float | None = None
    recurring_cost: float | None = None
    currency: str = "KRW"
    account_ref: str | None = None
    portal_url: str | None = None
    domain: str | None = None                    # 서비스 도메인
    homepage_url: str | None = None              # 홈페이지·관련 링크
    tech_info: str | None = None                 # IT 정보(리전·티어 등)
    contract_id: int | None = None
    owner_employee_id: int | None = None         # 서비스 담당자
    sales_contact: str | None = None             # 영업 담당자
    cost_center: str | None = None
    cost_period: str | None = None               # MONTHLY|ANNUAL 예상비용 단위
    vat_included: bool | None = None             # 부가세 포함 여부
    usage_unit: str | None = None                # 결제 단위(토큰 등)
    registered_on: date | None = None
    started_on: date | None = None
    first_billing_on: date | None = None
    payment_terms: str | None = None
    expires_on: date | None = None
    auto_renew: bool = False
    cancel_notice_days: int | None = None
    license_key: str | None = None
    api_token: str | None = None                 # API 토큰(암호화 저장)
    intangible_asset_id: int | None = None       # finance.IntangibleAsset 연결(감가상각·F-5)
    note: str | None = None


class ServicePatch(BaseModel):
    name: str | None = None
    service_type: str | None = None
    billing_model: str | None = None
    billing_cycle: str | None = None
    vendor: str | None = None
    edition: str | None = None
    seats: int | None = None
    license_metric: str | None = None
    alloc_basis: str | None = None
    unit_cost: float | None = None
    recurring_cost: float | None = None
    currency: str | None = None
    account_ref: str | None = None
    portal_url: str | None = None
    domain: str | None = None
    homepage_url: str | None = None
    tech_info: str | None = None
    contract_id: int | None = None
    owner_employee_id: int | None = None
    sales_contact: str | None = None
    cost_center: str | None = None
    cost_period: str | None = None
    vat_included: bool | None = None
    usage_unit: str | None = None
    registered_on: date | None = None
    started_on: date | None = None
    first_billing_on: date | None = None
    payment_terms: str | None = None
    expires_on: date | None = None
    auto_renew: bool | None = None
    cancel_notice_days: int | None = None
    license_key: str | None = None
    api_token: str | None = None
    intangible_asset_id: int | None = None
    note: str | None = None


class AllocBody(BaseModel):
    employee_id: int | None = None
    it_asset_id: int | None = None
    note: str | None = None


def _svc_dict(s: SoftwareService) -> dict:
    return {
        "id": s.id, "name": s.name, "vendor": s.vendor, "edition": s.edition,
        "status": s.status, "cancelled_on": s.cancelled_on,
        "service_type": s.service_type, "billing_model": s.billing_model,
        "billing_cycle": s.billing_cycle, "seats": s.seats, "license_metric": s.license_metric,
        "alloc_basis": s.alloc_basis, "currency": s.currency,
        "unit_cost": float(s.unit_cost) if s.unit_cost is not None else None,
        "recurring_cost": float(s.recurring_cost) if s.recurring_cost is not None else None,
        "account_ref": s.account_ref, "portal_url": s.portal_url, "contract_id": s.contract_id,
        "domain": s.domain, "homepage_url": s.homepage_url, "tech_info": s.tech_info,
        "owner_employee_id": s.owner_employee_id, "sales_contact": s.sales_contact,
        "cost_center": s.cost_center, "cost_period": s.cost_period,
        "vat_included": s.vat_included, "usage_unit": s.usage_unit,
        "registered_on": s.registered_on, "started_on": s.started_on,
        "first_billing_on": s.first_billing_on, "payment_terms": s.payment_terms,
        "expires_on": s.expires_on, "auto_renew": s.auto_renew,
        "cancel_notice_days": s.cancel_notice_days, "intangible_asset_id": s.intangible_asset_id,
        "key_masked": _sw.masked_key(s), "token_masked": _sw.masked_token(s), "note": s.note,
    }


@router.post("/services", status_code=201)
async def create_service(body: ServiceBody, user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    s = await _sw.create(session, actor=user.user_id, **body.model_dump())
    await session.commit()
    return _svc_dict(s)


@router.get("/services")
async def list_services(service_type: str | None = None, status: str | None = None,
                        q: str | None = None,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    items = await _sw.list_services(session, service_type=service_type, status=status, q=q)
    out = []
    for s in items:
        comp = await _sw.compliance(session, s.id)
        out.append({**_svc_dict(s), "compliance": comp})
    return {"items": out}


@router.get("/services/{service_id}")
async def get_service(service_id: int, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    s = await _sw.get(session, service_id)
    comp = await _sw.compliance(session, service_id)
    allocs = await _sw.allocations(session, service_id)
    names = await _svc.employee_names(session, [a.employee_id for a in allocs])
    return {**_svc_dict(s), "compliance": comp, "allocations": [
        {"id": a.id, "employee_id": a.employee_id,
         "employee_name": (names.get(a.employee_id) or {}).get("name"),
         "it_asset_id": a.it_asset_id,
         "allocated_on": a.allocated_on, "released_on": a.released_on} for a in allocs]}


@router.patch("/services/{service_id}")
async def patch_service(service_id: int, body: ServicePatch,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    s = await _sw.update(session, actor=user.user_id, service_id=service_id, **fields)
    await session.commit()
    return _svc_dict(s)


@router.post("/services/{service_id}/cancel")
async def cancel_service(service_id: int, reactivate: bool = False,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """서비스 해지/재개(F-2) — 해지 시 비용 점검·컴플라이언스 대시보드에서 제외."""
    _require(user, "write")
    s = await _sw.cancel(session, actor=user.user_id, service_id=service_id, reactivate=reactivate)
    await session.commit()
    return _svc_dict(s)


@router.post("/services/{service_id}/allocate")
async def allocate_seat(service_id: int, body: AllocBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    a = await _sw.allocate(session, actor=user.user_id, service_id=service_id,
                           employee_id=body.employee_id, it_asset_id=body.it_asset_id, note=body.note)
    await session.commit()
    return {"allocation_id": a.id}


@router.post("/service-allocations/{allocation_id}/release")
async def release_seat(allocation_id: int, user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    await _sw.release(session, actor=user.user_id, allocation_id=allocation_id)
    await session.commit()
    return {"ok": True}


# ── 비용 원장·주기 점검 (M2b) ────────────────────────────────
class CostBody(BaseModel):
    period: str
    amount: float
    currency: str = "KRW"
    fx_rate: float | None = None
    usage_note: str | None = None
    source: str = "MANUAL"


class ConfirmBody(BaseModel):
    note: str | None = None


@router.post("/services/{service_id}/costs")
async def record_cost(service_id: int, body: CostBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    c = await _cost.record_cost(session, actor=user.user_id, service_id=service_id,
                                **body.model_dump())
    await session.commit()
    return {"id": c.id, "period": c.period, "amount_krw": float(c.amount_krw)}


@router.get("/services/{service_id}/costs")
async def list_costs(service_id: int, user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    rows = await _cost.costs(session, service_id)
    return {"items": [
        {"period": c.period, "amount": float(c.amount), "currency": c.currency,
         "fx_rate": float(c.fx_rate) if c.fx_rate else None, "amount_krw": float(c.amount_krw),
         "usage_note": c.usage_note, "source": c.source,
         "posted": c.posted_at is not None} for c in rows]}


@router.get("/opex")
async def opex(period: str, user: CurrentUser = Depends(get_current_user),
               session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    return await _cost.opex_summary(session, period)


@router.post("/cost-reviews/run")
async def run_cost_review(period: str, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """월 비용 점검 실행 — 전월 대비 변동·미입력 감지·플래그."""
    _require(user, "write")
    out = await _cost.run_review(session, actor=user.user_id, period=period)
    await session.commit()
    return out


@router.get("/cost-reviews")
async def list_cost_reviews(period: str, status: str | None = None,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    rows = await _cost.list_reviews(session, period=period, status=status)
    return {"items": [
        {"id": rv.id, "service_id": rv.service_id, "service_name": s.name,
         "service_type": s.service_type, "vendor": s.vendor,
         "prev_amount": float(rv.prev_amount) if rv.prev_amount is not None else None,
         "amount": float(rv.amount) if rv.amount is not None else None,
         "delta_pct": float(rv.delta_pct) if rv.delta_pct is not None else None,
         "reason": rv.reason, "status": rv.status, "note": rv.note}
        for rv, s in rows]}


@router.post("/cost-reviews/{review_id}/confirm")
async def confirm_cost_review(review_id: int, body: ConfirmBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    await _cost.confirm_review(session, actor=user.user_id, review_id=review_id, note=body.note)
    await session.commit()
    return {"ok": True}


# ── IT 예산 관리(R4·F-6) ─────────────────────────────────────
class BudgetBody(BaseModel):
    period: str                       # yyyy-mm
    cost_center: str | None = None    # 미지정 = 전체 IT 총예산
    amount_krw: float
    note: str | None = None


def _budget_dict(b) -> dict:
    return {"id": b.id, "period": b.period, "cost_center": b.cost_center,
            "amount_krw": float(b.amount_krw), "note": b.note}


@router.post("/budgets", status_code=201)
async def upsert_budget(body: BudgetBody, user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    b = await _cost.upsert_budget(session, actor=user.user_id, period=body.period,
                                  cost_center=body.cost_center, amount_krw=body.amount_krw,
                                  note=body.note)
    await session.commit()
    return _budget_dict(b)


@router.get("/budgets")
async def list_budgets(period: str, user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    _require(user, "read")
    rows = await _cost.list_budgets(session, period)
    return {"items": [_budget_dict(b) for b in rows]}


@router.delete("/budgets/{budget_id}")
async def delete_budget(budget_id: int, user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    await _cost.delete_budget(session, actor=user.user_id, budget_id=budget_id)
    await session.commit()
    return {"ok": True}


@router.get("/budget-variance")
async def budget_variance(period: str, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """월 예산 대비 실적 — 코스트센터별·전체·초과·미예산 지출."""
    _require(user, "read")
    return await _cost.budget_variance(session, period)


# ── IT 비용 GL 전표 전기(R4·F-6) ─────────────────────────────
@router.post("/cost-postings/run")
async def run_cost_posting(period: str, user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """월 IT 비용 GL 전기 — 건별 전표(DR 정보시스템운영비/CR 미지급금).

    재검토 MED(SoD) — GL 전표 생성은 **재무 권한**(finance.vouchers write)으로 통제.
    비용 입력(itam.assets write)과 전기(회계) 주체 분리 — IT는 비용 입력, 회계가 전기.
    """
    require_scope(user, "finance.vouchers", "write")
    out = await _cost.post_period(session, actor=user.user_id, period=period)
    await session.commit()
    return out


@router.get("/cost-postings")
async def cost_posting_status(period: str, user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """월 전기 현황 — 전기/미전기 건수·금액."""
    _require(user, "read")
    return await _cost.posting_status(session, period)


@router.get("/shadow-it")
async def shadow_it(months: int = 12, user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """Shadow-IT 대사 — 미지급금·세금계산서(매입) 중 ITAM 미등록 IT 벤더 지출 탐지."""
    _require(user, "read")
    return await _cost.shadow_it_reconcile(session, months=months)
