"""HR 라우터 — design §4.3. 행 수준 scope는 서비스 입력 계약으로 강제 (§6.2)."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.months import YearMonth
from erp.common.policy import ScopeKind, require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.errors import ForbiddenError, ValidationError
from erp.hr.absence_service import AbsenceService
from erp.hr.compensation_service import CompensationService
from erp.hr.duty_service import DutyCell, DutyService
from erp.hr.employee_service import CertificateService, EmployeeService, LicenseService, PayrollService
from erp.hr.leave_service import LeaveService
from erp.hr.models import AttendanceSummary, Employee, PayrollLine, PayrollRecord

router = APIRouter(prefix="/hr", tags=["hr"])
_emp, _lic, _leave = EmployeeService(), LicenseService(), LeaveService()
_duty, _pay, _cert = DutyService(), PayrollService(), CertificateService()
_comp = CompensationService()
_absence = AbsenceService()
_audit = AuditService()


def _scope(user: CurrentUser, resource: str, action: str):
    """단일 권한 게이트 위임 (Check#4 — M5 수렴)."""
    return require_scope(user, resource, action)


# ── 직원 (FR-03) ───────────────────────────────────────────
class EmployeeCreate(BaseModel):
    employee_no: str
    name: str
    hired_on: date
    department_id: int | None = None
    position: str | None = None
    job_group: str = "STAFF"
    birth_date: date | None = None
    phone: str | None = None
    address: str | None = None
    # 직군·직종·고용형태 (의료기관 분리 — ERP 생성 직원은 source=ERP 자동)
    staff_class: Literal["MEDICAL", "SUPPORT"] | None = None
    job_category: str | None = None
    employment_type: Literal["REGULAR", "CONTRACT", "DAILY", "TEMP", "WATCH"] = "REGULAR"
    contract_start: date | None = None
    contract_end: date | None = None
    daily_wage: float | None = None


@router.get("/employees")
async def list_employees(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session),
                         page: int = 1, size: int = 50,
                         q: str | None = None, department_id: int | None = None,
                         staff_class: str | None = None, employment_type: str | None = None,
                         source: str | None = None):
    scope = _scope(user, "hr.employees", "read")
    size = min(size, 200)
    stmt = select(Employee).order_by(Employee.id)
    # caller_scope 명시 합성 (design §6.2 — 자동 주입 단독 의존 금지)
    if scope.kind == ScopeKind.DEPT:
        stmt = stmt.where(Employee.department_id == scope.dept_id)
    elif scope.kind == ScopeKind.SELF:
        stmt = stmt.where(Employee.id == _self_employee_subq(user))
    if q:                                          # 이름·사번 부분 검색
        stmt = stmt.where(Employee.name.ilike(f"%{q}%") | Employee.employee_no.ilike(f"%{q}%"))
    if department_id is not None:
        stmt = stmt.where(Employee.department_id == department_id)
    if staff_class:                                # 의료진/일반직 분리 (의료기관)
        stmt = stmt.where(Employee.staff_class == staff_class)
    if employment_type:
        stmt = stmt.where(Employee.employment_type == employment_type)
    if source:                                     # HIS 미러 / ERP 관리 분리
        stmt = stmt.where(Employee.source == source)
    rows = (await session.execute(
        stmt.offset((page - 1) * size).limit(size))).scalars().all()
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="hr.employees", route="/hr/employees")
    return {"items": [_emp_dto(e) for e in rows], "page": page, "size": size}


def _self_employee_q(user: CurrentUser):
    """본인 employee_id 조회용 SELECT (실행용 — SelectBase.select() deprecated 회피)."""
    from erp.common.models import User as U
    return select(U.employee_id).where(U.id == user.user_id)


def _self_employee_subq(user: CurrentUser):
    return _self_employee_q(user).scalar_subquery()   # WHERE 절 상관 서브쿼리용


async def _assert_employee_access(session, user: CurrentUser, scope, employee_id: int) -> None:
    """행 scope 합성 (§6.2) — SELF는 본인, DEPT는 본인부서 직원만. 급여정보 타인 접근 차단."""
    if scope.kind == ScopeKind.ALL:
        return
    if scope.kind == ScopeKind.SELF:
        self_id = (await session.execute(
            _self_employee_q(user))).scalar_one_or_none()
        if self_id != employee_id:
            raise ForbiddenError("본인 정보만 조회할 수 있습니다")
    elif scope.kind == ScopeKind.DEPT:
        emp = await session.get(Employee, employee_id)
        if emp is None or emp.department_id != user.dept_id:
            raise ForbiddenError("본인 부서 직원만 조회할 수 있습니다")
    else:
        raise ForbiddenError()


def _require_all(scope) -> None:
    """전사 단위 목록은 ALL 스코프만 — SELF/DEPT로 행 필터를 걸 수 없는 집계·목록용.

    2026-10-09 전체 흐름 검토 G — `_scope(...)` 반환값을 버린 채 전사 데이터를 돌려주던 엔드포인트가
    STAFF(SELF)에게 전 직원 징계·계약·퇴직충당을 노출했다."""
    if scope.kind != ScopeKind.ALL:
        raise ForbiddenError("전사 조회 권한(관리자)이 필요합니다")


def _emp_dto(e: Employee) -> dict:
    from erp.common.crypto import mask_phone
    phone = None
    if e.phone_enc is not None:
        phone = mask_phone(EmployeeService().decrypted_phone(e) or "")  # 마스킹 기본 (NFR)
    return {"id": e.id, "employee_no": e.employee_no, "name": e.name,
            "department_id": e.department_id, "position": e.position,
            "job_group": e.job_group, "hired_on": e.hired_on, "phone": phone,
            "source": e.source, "staff_class": e.staff_class,
            "job_category": e.job_category, "employment_type": e.employment_type}


@router.get("/employees/{employee_id}")
async def employee_detail(employee_id: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """직원 상세 — 발령·면허 탭 (design §7.1). scope 검증 후 접속기록."""
    scope = _scope(user, "hr.employees", "read")
    from erp.hr.models import Appointment, License
    emp = (await session.execute(
        select(Employee).where(Employee.id == employee_id))).scalar_one_or_none()
    if emp is None:
        from erp.errors import ValidationError
        raise ValidationError("존재하지 않는 직원입니다")
    if scope.kind == ScopeKind.DEPT and emp.department_id != scope.dept_id:
        raise ForbiddenError("본인 부서 직원만 조회할 수 있습니다")
    if scope.kind == ScopeKind.SELF:
        my_emp = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
        if my_emp != employee_id:
            raise ForbiddenError()
    appointments = (await session.execute(
        select(Appointment).where(Appointment.employee_id == employee_id)
        .order_by(Appointment.effective_on))).scalars().all()
    licenses = (await session.execute(
        select(License).where(License.employee_id == employee_id))).scalars().all()
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="hr.employees", target_id=str(employee_id),
                               route=f"/hr/employees/{employee_id}")
    return {
        **_emp_dto(emp),
        "appointments": [{"type": a.appointment_type, "from_dept": a.from_department_id,
                          "to_dept": a.to_department_id, "to_position": a.to_position,
                          "effective_on": a.effective_on} for a in appointments],
        "licenses": [{"type": lc.license_type, "no": lc.license_no,
                      "expires_on": lc.expires_on} for lc in licenses],
    }


@router.get("/duties/me")
async def my_duties(yyyymm: YearMonth,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """본인 듀티 조회 (design §7.1 — 직원 R(본인))."""
    from erp.hr.models import DutyAssignment, DutySchedule
    emp_id = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
    if emp_id is None:
        raise ForbiddenError("직원 정보가 연결되지 않은 계정입니다")
    rows = (await session.execute(
        select(DutyAssignment)
        .join(DutySchedule, DutySchedule.id == DutyAssignment.schedule_id)
        .where(DutySchedule.yyyymm == yyyymm, DutyAssignment.employee_id == emp_id)
        .order_by(DutyAssignment.duty_on))).scalars().all()
    return {"items": [{"duty_on": r.duty_on, "shift": r.shift} for r in rows]}


@router.get("/duties/{yyyymm}/export.xlsx")
async def export_duties(yyyymm: YearMonth, department_id: int | None = None,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """월 근무표 Excel 내보내기 (FR-25) — 직원×일자 D/E/N 그리드. 부서 scope 합성."""
    import calendar

    from fastapi.responses import StreamingResponse

    from erp.common.excel import build_xlsx
    from erp.hr.models import DutyAssignment, DutySchedule

    scope = _scope(user, "hr.employees", "read")
    if scope.kind in (ScopeKind.NONE, ScopeKind.SELF):
        raise ForbiddenError()
    q = (select(DutyAssignment.duty_on, DutyAssignment.shift,
                Employee.employee_no, Employee.name)
         .join(DutySchedule, DutySchedule.id == DutyAssignment.schedule_id)
         .join(Employee, Employee.id == DutyAssignment.employee_id)
         .where(DutySchedule.yyyymm == yyyymm))
    if scope.kind == ScopeKind.DEPT:           # 행 scope 강제 (§6.2)
        q = q.where(DutySchedule.department_id == scope.dept_id)
    elif department_id is not None:
        q = q.where(DutySchedule.department_id == department_id)
    rows = (await session.execute(
        q.order_by(Employee.employee_no, DutyAssignment.duty_on))).all()

    year, month = int(yyyymm[:4]), int(yyyymm[5:7])
    days = calendar.monthrange(year, month)[1]
    grid: dict[str, dict] = {}
    for duty_on, shift, emp_no, name in rows:
        g = grid.setdefault(emp_no, {"name": name, "cells": {}})
        g["cells"][duty_on.day] = shift
    header = ["사번", "이름", *[str(d) for d in range(1, days + 1)]]
    data = [[emp_no, g["name"], *[g["cells"].get(d, "") for d in range(1, days + 1)]]
            for emp_no, g in sorted(grid.items())]

    await _audit.record_access(session, actor=user.user_id, action="EXPORT",
                               entity="duties", route=f"/hr/duties/{yyyymm}/export.xlsx")
    # 시트명 한글 통일(타 반출과 컨벤션 정합) — 파일명(duty-*.xlsx)은 영문 케밥 유지
    content = build_xlsx(sheet_name=f"근무표-{yyyymm}", headers=header, rows=data)
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="duty-{yyyymm}.xlsx"'})


@router.post("/employees", status_code=201)
async def create_employee(body: EmployeeCreate,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.employees", "write")
    emp = await _emp.create(session, actor=user.user_id, **body.model_dump())
    return {"id": emp.id}


class TransferBody(BaseModel):
    to_department_id: int | None
    to_position: str | None
    effective_on: date
    note: str | None = None


@router.post("/employees/{employee_id}/appointments", status_code=201)
async def transfer(employee_id: int, body: TransferBody,
                   user: CurrentUser = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.employees", "write")
    appt = await _emp.transfer(session, actor=user.user_id, employee_id=employee_id,
                               **body.model_dump())
    return {"id": appt.id}


# ── 면허 (FR-04) ───────────────────────────────────────────
class LicenseBody(BaseModel):
    license_type: str
    license_no: str
    issued_on: date | None = None
    expires_on: date | None = None


@router.post("/employees/{employee_id}/licenses", status_code=201)
async def add_license(employee_id: int, body: LicenseBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.employees", "write")
    lic = await _lic.add(session, employee_id=employee_id, **body.model_dump())
    return {"id": lic.id}


class TrainingBody(BaseModel):
    completed_on: date


@router.patch("/licenses/{license_id}/training")
async def record_license_training(license_id: int, body: TrainingBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """보수교육(면허 신고) 이수 기록 — 차기 기한(+3년) 재기산·D-60/30/0 경보 기준."""
    _scope(user, "hr.employees", "write")
    lic = await _lic.record_training(session, license_id=license_id,
                                     completed_on=body.completed_on)
    return {"id": lic.id, "training_completed_on": lic.training_completed_on}


# ── 휴가 (FR-05/31) ────────────────────────────────────────
class LeaveRequestBody(BaseModel):
    leave_type: str = "ANNUAL"
    start_on: date
    end_on: date
    days: float
    reason: str | None = None


@router.post("/leaves/requests", status_code=201)
async def request_leave(body: LeaveRequestBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    # 본인 신청 — employee_id는 토큰의 사용자에서 (위조 불가)
    emp_id = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
    if emp_id is None:
        raise ForbiddenError("직원 정보가 연결되지 않은 계정입니다")
    req = await _leave.request(session, employee_id=emp_id, **body.model_dump())
    await session.flush()
    # 전자결재 연동(설정 시) — HIS W.Sign 상신. 실패해도 신청은 유지(다음 회차 재상신).
    try:
        from erp.hr.eapproval_service import EApprovalService
        await EApprovalService().submit_leave(session, leave=req)
    except Exception as exc:  # noqa: BLE001 — 결재 상신 실패가 휴가신청을 막지 않음
        import logging
        logging.getLogger("erp.hr.eapproval").warning(
            "휴가 결재 상신 실패(신청은 유지)", extra={"ctx": {"leave": req.id, "err": str(exc)[:120]}})
    return {"id": req.id, "status": req.status, "approval_id": req.approval_id,
            "view_url": req.view_url}


@router.patch("/leaves/requests/{request_id}/approve")
async def approve_leave(request_id: int,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    scope = _scope(user, "hr.leaves", "approve")
    req = await _leave.decide(session, request_id=request_id, approve=True,
                              decider_user_id=user.user_id, decider_dept_id=user.dept_id,
                              decider_scope_dept_only=(scope.kind == ScopeKind.DEPT))
    return {"id": req.id, "status": req.status}


@router.patch("/leaves/requests/{request_id}/reject")
async def reject_leave(request_id: int,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    scope = _scope(user, "hr.leaves", "approve")
    req = await _leave.decide(session, request_id=request_id, approve=False,
                              decider_user_id=user.user_id, decider_dept_id=user.dept_id,
                              decider_scope_dept_only=(scope.kind == ScopeKind.DEPT))
    return {"id": req.id, "status": req.status}


@router.get("/leaves/requests")
async def list_leave_requests(status: str | None = None,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session),
                              page: int = 1, size: int = 50):
    """휴가 신청 목록 (W3) — 부서장=본부서, 직원=본인 (행 scope 명시 합성)."""
    scope = _scope(user, "hr.leaves", "read")
    from erp.hr.models import LeaveRequest
    size = min(size, 200)
    q = select(LeaveRequest, Employee.name, Employee.department_id) \
        .join(Employee, Employee.id == LeaveRequest.employee_id)
    if status:
        q = q.where(LeaveRequest.status == status)
    if scope.kind == ScopeKind.DEPT:
        q = q.where(Employee.department_id == scope.dept_id)
    elif scope.kind == ScopeKind.SELF:
        q = q.where(LeaveRequest.employee_id == _self_employee_subq(user))
    rows = (await session.execute(
        q.order_by(LeaveRequest.id.desc()).offset((page - 1) * size).limit(size))).all()
    return {"items": [
        {"id": r.LeaveRequest.id, "employee_name": r.name,
         "department_id": r.department_id, "leave_type": r.LeaveRequest.leave_type,
         "start_on": r.LeaveRequest.start_on, "end_on": r.LeaveRequest.end_on,
         "days": float(r.LeaveRequest.days), "status": r.LeaveRequest.status,
         "reason": r.LeaveRequest.reason} for r in rows],
        "page": page, "size": size}


@router.get("/leaves/remaining")
async def remaining_leave(year: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    emp_id = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
    if emp_id is None:
        raise ForbiddenError()
    remaining = await _leave.remaining_days(session, employee_id=emp_id, year=year)
    return {"year": year, "remaining": remaining}


@router.post("/leaves/grant-annual")
async def grant_annual_leave(year: int,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """재직 전원 당해연도 연차 자동 부여 (관리자·멱등) — 미배선 결함 백필/신규입사 스윕.

    스케줄러(job_annual_leave_grant)가 연 1회 자동 실행하나, 도입 시점 백필·즉시 재동기화용 수동 EP."""
    _scope(user, "hr.employees", "write")
    out = await _leave.grant_annual_all(session, year=year)
    await session.commit()
    return out


# ── 듀티 (FR-06/32) ────────────────────────────────────────
class DutyCellBody(BaseModel):
    employee_id: int
    duty_on: date
    shift: str


class DutySaveBody(BaseModel):
    department_id: int
    cells: list[DutyCellBody]


class DutyGenerateBody(BaseModel):
    department_id: int
    job_group: str = "NURSE"


@router.post("/duties/{yyyymm}/generate")
async def generate_duty(yyyymm: YearMonth, body: DutyGenerateBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """자동 근무표 생성 (FR-45) — 직군 템플릿 기반. 위반은 경고 반환(검토 후 publish)."""
    from erp.hr.duty_generator import DutyGenerator

    scope = _scope(user, "hr.duties", "write")
    if scope.kind == ScopeKind.DEPT and body.department_id != scope.dept_id:
        raise ForbiddenError("본인 부서의 듀티만 편성할 수 있습니다")
    schedule, violations, headcount = await DutyGenerator().generate(
        session, department_id=body.department_id, yyyymm=yyyymm, job_group=body.job_group)
    return {"schedule_id": schedule.id, "status": schedule.status, "headcount": headcount,
            "violations": [{"rule": v.rule, "employee_id": v.employee_id, "detail": v.detail}
                           for v in violations]}


@router.get("/duties/{yyyymm}")
async def load_duty(yyyymm: YearMonth, department_id: int,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """저장된 월 근무표 조회 — 편성 화면 재진입 시 기존 배정 복원.

    이 EP가 없어 화면이 항상 빈 그리드로 시작했고, PUT이 delete-then-insert라 그대로 저장하면
    기존 배정이 전량 삭제됐다. 읽기지만 편성 화면 전용이므로 **쓰기와 동일한 부서 경계**를 건다.
    """
    scope = _scope(user, "hr.duties", "write")
    if scope.kind == ScopeKind.DEPT and department_id != scope.dept_id:
        raise ForbiddenError("본인 부서의 듀티만 조회할 수 있습니다")
    return await _duty.load_month(session, department_id=department_id, yyyymm=yyyymm)


@router.put("/duties/{yyyymm}")
async def save_duty(yyyymm: YearMonth, body: DutySaveBody,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    scope = _scope(user, "hr.duties", "write")
    if scope.kind == ScopeKind.DEPT and body.department_id != scope.dept_id:
        raise ForbiddenError("본인 부서의 듀티만 편성할 수 있습니다")
    schedule, violations = await _duty.save_month(
        session, department_id=body.department_id, yyyymm=yyyymm,
        cells=[DutyCell(c.employee_id, c.duty_on, c.shift) for c in body.cells])
    return {"schedule_id": schedule.id, "status": schedule.status,
            "violations": [{"rule": v.rule, "employee_id": v.employee_id,
                            "detail": v.detail} for v in violations]}  # FR-32 경고


# ── 급여 (FR-07) ───────────────────────────────────────────
class PayrollLineBody(BaseModel):
    item_code: str
    item_kind: str
    amount: float


class PayrollBody(BaseModel):
    employee_id: int
    lines: list[PayrollLineBody]


@router.put("/payrolls/{yyyymm}")
async def upsert_payroll(yyyymm: YearMonth, body: PayrollBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.payroll", "write")
    record = await _pay.upsert_lines(session, actor=user.user_id,
                                     employee_id=body.employee_id, yyyymm=yyyymm,
                                     lines=[line.model_dump() for line in body.lines])
    return {"record_id": record.id}


# ── 의료진 개별 계약 (P4-1) ──────────────────────────────
class MedicalContractBody(BaseModel):
    employee_id: int
    valid_from: date
    base_pay: float
    duty_pay: float | None = None
    incentive_type: Literal["NONE", "NET_COLLECTION", "DEPT_SHARE", "TARGET_EXCESS", "RVU"] = "NONE"
    incentive_rate: float | None = None
    incentive_params: dict = {}
    exclusive: bool = True
    require_approval: bool = False     # True면 DRAFT 결재 경로(승인·발효 후 ACTIVE)
    note: str | None = None


def _contract_dto(c) -> dict:
    return {"id": c.id, "employee_id": c.employee_id, "valid_from": c.valid_from,
            "valid_to": c.valid_to, "effective_on": getattr(c, "effective_on", None),
            "base_pay": float(c.base_pay),
            "duty_pay": float(c.duty_pay) if c.duty_pay is not None else None,
            "incentive_type": c.incentive_type,
            "incentive_rate": float(c.incentive_rate) if c.incentive_rate is not None else None,
            "incentive_params": c.incentive_params, "exclusive": c.exclusive,
            "status": c.status, "approved_by": getattr(c, "approved_by", None),
            "terminate_reason": getattr(c, "terminate_reason", None),
            "terminated_on": getattr(c, "terminated_on", None), "note": c.note,
            # 전자서명 미러(e-sign #6) — HIS sign 결과 + 회수 완료본(Attachment)
            "sign_status": c.sign_status, "signed_at": c.signed_at,
            "sign_cert_serial": c.sign_cert_serial, "sign_doc_ref": c.sign_doc_ref,
            "sign_doc_attachment_id": c.sign_doc_attachment_id}


@router.post("/medical-contracts", status_code=201)
async def create_medical_contract(body: MedicalContractBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """의료진 개별 계약 등록 (보수=민감, hr.payroll 권한). 기존 ACTIVE 계약 자동 승계."""
    from erp.hr.contract_service import MedicalContractService
    _scope(user, "hr.payroll", "write")
    c = await MedicalContractService().create(session, actor=user.user_id, **body.model_dump())
    return {"id": c.id, "status": c.status}


@router.get("/medical-contracts")
async def list_medical_contracts(employee_id: int,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """직원 의료진 계약 이력 (보수=민감)."""
    from erp.hr.contract_service import MedicalContractService
    scope = _scope(user, "hr.payroll", "read")
    await _assert_employee_access(session, user, scope, employee_id)  # IDOR — 타인 보수 차단
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="hr.medical_contracts", target_id=str(employee_id))
    items = await MedicalContractService().list_for(session, employee_id=employee_id)
    return {"items": [_contract_dto(c) for c in items]}


def _incentive_dto(ic) -> dict:
    return {"id": ic.id, "employee_id": ic.employee_id, "period": ic.period,
            "contract_id": ic.contract_id, "net_revenue": float(ic.net_revenue),
            "amount": float(ic.amount), "status": ic.status, "snapshot": ic.snapshot,
            "voucher_ref": ic.voucher_ref, "note": ic.note,
            "reviewed_by": ic.reviewed_by, "approved_by": ic.approved_by}


@router.get("/incentive/calculate")
async def calculate_incentive(employee_id: int, period: YearMonth,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """성과급 산정 미리보기 (P4-3) — 계약 산식 × 진료실적(등록·승인 없이). hr.payroll."""
    from erp.hr.incentive_service import IncentiveService
    scope = _scope(user, "hr.payroll", "read")
    await _assert_employee_access(session, user, scope, employee_id)  # IDOR — 타인 성과급 차단
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="hr.incentive", target_id=str(employee_id))
    return await IncentiveService().calculate(session, employee_id=employee_id, period=period)


@router.get("/incentive/dashboard")
async def incentive_dashboard(period: YearMonth, user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """성과급 대시보드 (P4-5) — 의료진별 실적·미리보기·산정 상태·총계. hr.payroll."""
    from erp.hr.incentive_service import IncentiveService
    scope = _scope(user, "hr.payroll", "read")
    if scope.kind != ScopeKind.ALL:   # 전 의료진 집계 — SELF(직원) 무의미·차단(IDOR)
        raise ForbiddenError("전체 성과급 대시보드는 관리자 권한이 필요합니다")
    return await IncentiveService().dashboard(session, period=period)


@router.get("/incentive")
async def list_incentive(period: YearMonth, user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    from erp.hr.incentive_service import IncentiveService
    scope = _scope(user, "hr.payroll", "read")
    if scope.kind != ScopeKind.ALL:   # 전 의료진 성과급 목록 — SELF 무의미·차단(IDOR)
        raise ForbiddenError("전체 성과급 목록은 관리자 권한이 필요합니다")
    return {"items": [_incentive_dto(c) for c in
                      await IncentiveService().list_period(session, period=period)]}


class IncentiveRegisterBody(BaseModel):
    employee_id: int
    period: YearMonth


@router.post("/incentive/register", status_code=201)
async def register_incentive(body: IncentiveRegisterBody,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """성과급 산정 등록(DRAFT) — 근거 snapshot 보존."""
    from erp.hr.incentive_service import IncentiveService
    _scope(user, "hr.payroll", "write")
    ic = await IncentiveService().register(session, actor=user.user_id, **body.model_dump())
    return _incentive_dto(ic)


@router.post("/incentive/{calc_id}/review")
async def review_incentive(calc_id: int, user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    from erp.hr.incentive_service import IncentiveService
    _scope(user, "hr.payroll", "write")
    ic = await IncentiveService().review(session, actor=user.user_id, calc_id=calc_id)
    return _incentive_dto(ic)


@router.post("/incentive/{calc_id}/approve")
async def approve_incentive(calc_id: int, user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """성과급 승인 — PayrollLine(INCENTIVE) + 미지급 전표(급여비용/미지급금)."""
    from erp.hr.incentive_service import IncentiveService
    _scope(user, "hr.payroll", "write")
    ic = await IncentiveService().approve(session, actor=user.user_id, calc_id=calc_id)
    return _incentive_dto(ic)


@router.post("/incentive/{calc_id}/cancel")
async def cancel_incentive(calc_id: int, reason: str | None = None,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """성과급 취소/claw-back — 역분개 전표 + INCENTIVE 라인 제거."""
    from erp.hr.incentive_service import IncentiveService
    _scope(user, "hr.payroll", "write")
    ic = await IncentiveService().cancel(session, actor=user.user_id, calc_id=calc_id, reason=reason)
    return _incentive_dto(ic)


@router.get("/physician-revenue")
async def physician_revenue(period: YearMonth, doctor_id: int | None = None,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """의사별 진료실적 집계 (P4-2, 성과급 산정 입력) — 수납·청구·삭감. hr.payroll 권한."""
    from erp.hr.physician_revenue import PhysicianRevenueService
    scope = _scope(user, "hr.payroll", "read")
    if doctor_id is None:   # 전체 의사 집계 — SELF/DEPT(직원)엔 무의미·차단(IDOR)
        if scope.kind != ScopeKind.ALL:
            raise ForbiddenError("전체 의사 실적 집계는 관리자 권한이 필요합니다")
    else:                   # 특정 의사 — 본인/부서 경계 검증(형제 EP calculate와 동일 가드)
        await _assert_employee_access(session, user, scope, doctor_id)
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="hr.physician_revenue", target_id=str(doctor_id or "ALL"))
    return await PhysicianRevenueService().aggregate(session, period=period, doctor_id=doctor_id)


@router.post("/medical-contracts/{contract_id}/end")
async def end_medical_contract(contract_id: int, end_on: date,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    from erp.hr.contract_service import MedicalContractService
    _scope(user, "hr.payroll", "write")
    c = await MedicalContractService().end(session, actor=user.user_id,
                                           contract_id=contract_id, end_on=end_on)
    return {"id": c.id, "status": c.status}


# ── 의료계약 생애주기 (계약 통합 PR-2) — 위수탁계약과 일원화 ──────────
class MCTerminateBody(BaseModel):
    end_on: date | None = None
    reason: str = ""


class MCAmendPayBody(BaseModel):
    new_base_pay: float
    effective_on: date
    reason: str = ""


class MCRenewBody(BaseModel):
    new_valid_to: date


@router.post("/medical-contracts/{contract_id}/submit")
async def submit_medical_contract(contract_id: int,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """DRAFT→PENDING_APPROVAL 상신."""
    from erp.hr.contract_service import MedicalContractService
    _scope(user, "hr.payroll", "write")
    c = await MedicalContractService().submit_for_approval(session, actor=user.user_id,
                                                           contract_id=contract_id)
    return {"id": c.id, "status": c.status}


@router.post("/medical-contracts/{contract_id}/approve")
async def approve_medical_contract(contract_id: int, override: bool = False,
                                   user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """승인(직무분리 — 작성자 본인 불가). 결재선 룰 있으면 다단계.

    override=true는 최고경영자 전권(DIRECTOR 전용·엔진 재검증)."""
    from erp.hr.contract_service import MedicalContractService
    _scope(user, "hr.payroll", "write")
    c = await MedicalContractService().approve(session, actor=user.user_id,
                                               contract_id=contract_id, override=override)
    return {"id": c.id, "status": c.status,
            "pending_next_step": c.status == "PENDING_APPROVAL"}


@router.post("/medical-contracts/{contract_id}/reject")
async def reject_medical_contract(contract_id: int, body: MCTerminateBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    from erp.hr.contract_service import MedicalContractService
    _scope(user, "hr.payroll", "write")
    c = await MedicalContractService().reject(session, actor=user.user_id,
                                              contract_id=contract_id, reason=body.reason)
    return {"id": c.id, "status": c.status}


@router.post("/medical-contracts/{contract_id}/activate")
async def activate_medical_contract(contract_id: int,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """발효(APPROVED/DRAFT→ACTIVE·발효일 도래 검증·기존 ACTIVE 승계)."""
    from erp.hr.contract_service import MedicalContractService
    _scope(user, "hr.payroll", "write")
    c = await MedicalContractService().activate(session, actor=user.user_id,
                                                contract_id=contract_id)
    return {"id": c.id, "status": c.status}


@router.post("/medical-contracts/{contract_id}/terminate")
async def terminate_medical_contract(contract_id: int, body: MCTerminateBody,
                                     user: CurrentUser = Depends(get_current_user),
                                     session: AsyncSession = Depends(get_session)):
    from erp.hr.contract_service import MedicalContractService
    _scope(user, "hr.payroll", "write")
    c = await MedicalContractService().terminate(session, actor=user.user_id,
                                                 contract_id=contract_id,
                                                 end_on=body.end_on, reason=body.reason)
    return {"id": c.id, "status": c.status}


@router.post("/medical-contracts/{contract_id}/amend-pay")
async def amend_medical_contract_pay(contract_id: int, body: MCAmendPayBody,
                                     user: CurrentUser = Depends(get_current_user),
                                     session: AsyncSession = Depends(get_session)):
    """기본급 변경 — 변경이력(common.contract_amendments) 기록."""
    from erp.hr.contract_service import MedicalContractService
    _scope(user, "hr.payroll", "write")
    c = await MedicalContractService().amend_pay(session, actor=user.user_id,
                                                 contract_id=contract_id,
                                                 new_base_pay=body.new_base_pay,
                                                 effective_on=body.effective_on,
                                                 reason=body.reason)
    return {"id": c.id, "status": c.status, "base_pay": float(c.base_pay)}


@router.post("/medical-contracts/{contract_id}/renew")
async def renew_medical_contract(contract_id: int, body: MCRenewBody,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    from erp.hr.contract_service import MedicalContractService
    _scope(user, "hr.payroll", "write")
    c = await MedicalContractService().renew(session, actor=user.user_id,
                                             contract_id=contract_id,
                                             new_valid_to=body.new_valid_to)
    return {"id": c.id, "status": c.status, "valid_to": c.valid_to}


# ── 일반직 근로계약 (근기법 §17·P3-5) ──────────────────────────
def _emp_contract():
    from erp.hr.employment_contract_service import EmploymentContractService
    return EmploymentContractService()


class EmploymentContractBody(BaseModel):
    employee_id: int
    contract_type: str = "REGULAR"        # REGULAR|FIXED_TERM|PART_TIME
    valid_from: date
    base_pay: float
    valid_to: date | None = None
    weekly_hours: float | None = None
    work_schedule: str | None = None
    work_location: str | None = None
    job_duties: str | None = None
    note: str | None = None
    activate: bool = False                 # true=즉시 발효(기존 ACTIVE 승계)


@router.post("/employment-contracts", status_code=201)
async def create_employment_contract(body: EmploymentContractBody,
                                     user: CurrentUser = Depends(get_current_user),
                                     session: AsyncSession = Depends(get_session)):
    """일반직 근로계약 등록(근기법 §17 근로조건 명시). 의료진은 대상 아님."""
    _scope(user, "hr.payroll", "write")
    c = await _emp_contract().create(
        session, actor=user.user_id, employee_id=body.employee_id,
        contract_type=body.contract_type, valid_from=body.valid_from, base_pay=body.base_pay,
        valid_to=body.valid_to, weekly_hours=body.weekly_hours, work_schedule=body.work_schedule,
        work_location=body.work_location, job_duties=body.job_duties, note=body.note,
        activate=body.activate)
    return {"id": c.id, "status": c.status}


@router.get("/employment-contracts")
async def list_employment_contracts(employee_id: int,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """직원 근로계약 이력."""
    # 2026-10-09 전체 흐름 검토 G — STAFF(SELF)가 employee_id만 바꿔 타인 기본급·근로조건을 읽던 IDOR 차단
    scope = _scope(user, "hr.payroll", "read")
    await _assert_employee_access(session, user, scope, employee_id)
    return {"items": await _emp_contract().list_by_employee(session, employee_id=employee_id)}


@router.get("/employment-contracts/expiring")
async def expiring_employment_contracts(within_days: int = 30,
                                        user: CurrentUser = Depends(get_current_user),
                                        session: AsyncSession = Depends(get_session)):
    """만기 임박 계약직(갱신/종료 판단)."""
    _require_all(_scope(user, "hr.payroll", "read"))   # 2026-10-09 전체 흐름 검토 G — 전사 목록은 ALL만
    return {"items": await _emp_contract().list_expiring(session, within_days=within_days)}


@router.post("/employment-contracts/{contract_id}/activate")
async def activate_employment_contract(contract_id: int,
                                       user: CurrentUser = Depends(get_current_user),
                                       session: AsyncSession = Depends(get_session)):
    """발효 — DRAFT→ACTIVE(기존 ACTIVE 승계)."""
    _scope(user, "hr.payroll", "write")
    c = await _emp_contract().activate(session, actor=user.user_id, contract_id=contract_id)
    return {"id": c.id, "status": c.status}


class EmpContractEndBody(BaseModel):
    end_on: date


@router.post("/employment-contracts/{contract_id}/end")
async def end_employment_contract(contract_id: int, body: EmpContractEndBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """계약 종료."""
    _scope(user, "hr.payroll", "write")
    c = await _emp_contract().end(session, actor=user.user_id, contract_id=contract_id,
                                  end_on=body.end_on)
    return {"id": c.id, "status": c.status}


class EmpContractRenewBody(BaseModel):
    new_valid_to: date


@router.post("/employment-contracts/{contract_id}/renew")
async def renew_employment_contract(contract_id: int, body: EmpContractRenewBody,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """계약직 갱신 — 만료일 연장."""
    _scope(user, "hr.payroll", "write")
    c = await _emp_contract().renew(session, actor=user.user_id, contract_id=contract_id,
                                    new_valid_to=body.new_valid_to)
    return {"id": c.id, "status": c.status, "valid_to": str(c.valid_to) if c.valid_to else None}


# ── 직급·호봉 체계 + 징계·상벌 (Tier3·P3-8) ──────────────────────
def _paygrade():
    from erp.hr.personnel_service import PayGradeService
    return PayGradeService()


def _disciplinary():
    from erp.hr.personnel_service import DisciplinaryService
    return DisciplinaryService()


class PayGradeBody(BaseModel):
    grade: str
    step: int
    base_pay: float
    effective_year: int
    note: str | None = None


@router.post("/pay-grades", status_code=201)
async def upsert_pay_grade(body: PayGradeBody,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """호봉표 등록/갱신 — (직급,호봉,연도) 멱등. 관리자(hr.payroll)."""
    _scope(user, "hr.payroll", "write")
    r = await _paygrade().upsert_step(session, grade=body.grade, step=body.step,
                                      base_pay=body.base_pay, effective_year=body.effective_year,
                                      note=body.note)
    return {"id": r.id, "base_pay": float(r.base_pay)}


@router.get("/pay-grades")
async def list_pay_grades(effective_year: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """연도 호봉표."""
    _scope(user, "hr.payroll", "read")
    return {"items": await _paygrade().list_steps(session, effective_year=effective_year)}


@router.get("/pay-grades/lookup")
async def lookup_pay_grade(grade: str, step: int, year: int,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """직급·호봉·연도 → 기본급 조회."""
    _scope(user, "hr.payroll", "read")
    r = await _paygrade().lookup(session, grade=grade, step=step, year=year)
    if r is None:
        raise ValidationError("해당 호봉표 항목이 없습니다")
    return r


class DisciplinaryBody(BaseModel):
    employee_id: int
    action_type: str               # DISCIPLINE|REWARD
    category: str
    reason: str
    decided_on: date
    effective_from: date | None = None
    effective_to: date | None = None
    note: str | None = None


@router.post("/disciplinary-actions", status_code=201)
async def record_disciplinary(body: DisciplinaryBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """징계·상벌 등록 — 관리자(hr.payroll)."""
    _scope(user, "hr.payroll", "write")
    a = await _disciplinary().record(
        session, employee_id=body.employee_id, action_type=body.action_type,
        category=body.category, reason=body.reason, decided_on=body.decided_on,
        effective_from=body.effective_from, effective_to=body.effective_to,
        note=body.note, actor=user.user_id)
    return {"id": a.id, "action_type": a.action_type}


@router.get("/disciplinary-actions")
async def list_disciplinary(employee_id: int | None = None, action_type: str | None = None,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """상벌 이력 — 직원별 또는 최근(유형 필터)."""
    # 2026-10-09 전체 흐름 검토 G — 직원별은 행 scope(SELF=본인), 전사 최근 목록은 ALL만(징계 이력은 민감 인사정보)
    scope = _scope(user, "hr.payroll", "read")
    if employee_id is not None:
        await _assert_employee_access(session, user, scope, employee_id)
        return {"items": await _disciplinary().list_by_employee(session, employee_id=employee_id)}
    _require_all(scope)
    return {"items": await _disciplinary().recent(session, action_type=action_type)}


class PayrollGenerateBody(BaseModel):
    """월 변동분(work_days·night_hours·duty_count·ot_hours)은 미입력 시
    근태 집계·듀티에서 자동 산정(P1-②) — 명시 입력이 항상 우선(수동 보정)."""
    employee_id: int
    base_pay: float | None = None    # 미입력 시 보수 프로필에서 — 월 기본급(일용직은 daily_wage×work_days 자동)
    work_days: int | None = None     # 일용 근무일수(4대보험 적용기준·일용세)
    work_hours: float | None = None
    night_hours: float | None = None  # 간호사 야간·교대 시간
    duty_count: int | None = None     # 의사·전공의 당직 횟수
    ot_hours: float | None = None     # 연장근로 시간(통상시급×1.5) — **평일 연장만**(휴일근로 제외).
    # ⚠ 수동 입력 시 휴일근로 시간을 포함하지 말 것 — holiday_hours/holiday_ot_hours가 별도 가산되므로
    #   총연장(휴일 포함)으로 넣으면 이중 계상됨(L-3). 미입력 시 자동 산정이 휴일을 이미 분리.
    holiday_hours: float | None = None     # 휴일근로 8h 이내(×1.5) — 미입력 시 스테이징 자동(H-2)
    holiday_ot_hours: float | None = None  # 휴일근로 8h 초과(×2.0)
    unpaid_days: float | None = None  # 무급휴가 일수 — 일할 공제(미입력 시 UNPAID 휴가 자동)
    dependents: int | None = None    # 미입력 시 보수 프로필에서


async def _resolve_pay_basis(session: AsyncSession, emp: Employee,
                             base_pay: float | None, dependents: int | None) -> tuple[float, int]:
    """월별 입력이 없으면 마스터 폴백 — 기본급=보수 프로필,
    부양가족=가족 마스터(P3, 본인+counted) → 프로필 순. 미등록이면 온보딩 유도."""
    profile = None
    if base_pay is None or dependents is None:
        profile = await _comp.get(session, employee_id=emp.id)
    resolved_base = base_pay if base_pay is not None else float(profile.base_pay) if profile else 0.0
    if dependents is not None:
        resolved_dep = dependents
    else:
        from erp.hr.dependent_service import DependentService
        from_family = await DependentService().payroll_dependents(session, emp.id)
        resolved_dep = from_family if from_family is not None else (
            profile.dependents if profile else 1)
    if emp.employment_type != "DAILY" and resolved_base <= 0:
        raise ValidationError(
            "급여 기초정보 미등록 — 보수 프로필(기본급)을 먼저 등록하세요 (/hr/onboarding/pending)")
    return resolved_base, resolved_dep


def _pay_month(yyyymm: str) -> int:
    """급여월의 월 — 단일 소스 statutory.month_of("2026-07"·"202607")."""
    from erp.hr import statutory
    return statutory.month_of(yyyymm)


def _month_bounds(yyyymm: str) -> tuple[date, date]:
    """급여월 1일·말일 — "2026-07"·"202607" 모두 허용."""
    from calendar import monthrange
    d = yyyymm.replace("-", "")
    y, m = int(d[:4]), int(d[4:6])
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def _employed_days(emp: Employee, month_first: date, month_end: date) -> int:
    """해당월 재직 역일수 — max(입사일, 월초) ~ min(퇴사일, 월말) 양끝 포함(퇴사일=최종 근무일).

    2026-10-09 전체 흐름 검토 C — 입사·퇴사월 기본급 역일 일할의 분자. 0이면 해당월 재직 없음.
    """
    start = max(emp.hired_on, month_first) if emp.hired_on else month_first
    end = min(emp.resigned_on, month_end) if emp.resigned_on else month_end
    return max((end - start).days + 1, 0)


async def _withholding_inputs(session: AsyncSession, employee_ids: list[int], yyyymm: str) -> tuple:
    """급여 소득세 입력 — (활성 간이세액표|None, {직원: 8~20세 자녀 수}, {비과세 코드: 월 한도},
    {직원: 공제대상가족 수 — 부양가족 등록 있을 때만}) (v1.297.0). 일괄 생성도 개별 생성(_resolve_pay_basis)과 같은
    가족 수 규칙(등록=본인+산입 가족)을 쓴다 — 자녀는 등록에서 세는데 가족 수만 프로필이면 '가족 1·자녀 2'가 생긴다."""
    from erp.hr import withholding_table as wht
    from erp.hr.models import Dependent, PayItemCatalog
    first, _end = _month_bounds(yyyymm)
    table = await wht.load_active(session, on=first)
    deps: dict[int, list] = {}
    if employee_ids:
        for d in (await session.scalars(select(Dependent).where(Dependent.employee_id.in_(employee_ids)))).all():
            deps.setdefault(d.employee_id, []).append(d)
    fam = {e: wht.family_from(ds, None) for e, ds in deps.items()}
    kids = {e: wht.children_8_20(deps.get(e, []), first.year) for e in employee_ids} if table is not None else {}
    nontax = {c: (float(lim) if lim else None) for c, lim in (await session.execute(select(
        PayItemCatalog.code, PayItemCatalog.nontax_limit).where(
        PayItemCatalog.kind == "EARNING", PayItemCatalog.taxable.is_(False)))).all()}
    return table, kids, nontax, fam


async def _record_totals(session: AsyncSession, record_id: int) -> dict:
    """저장된 명세 기준 지급·공제·실지급 — 생성 결과(build_payroll)에 없는 소급정산 공제(NP_ADJ…)·성과급 줄까지 포함."""
    await session.flush()
    rows = (await session.execute(select(PayrollLine.item_kind, func.sum(PayrollLine.amount)).where(
        PayrollLine.record_id == record_id).group_by(PayrollLine.item_kind))).all()
    by = {k: int(v or 0) for k, v in rows}
    gross, ded = by.get("EARNING", 0), by.get("DEDUCTION", 0)
    return {"gross": gross, "deduction_total": ded, "net": gross - ded}


@router.post("/payrolls/{yyyymm}/generate")
async def generate_payroll(yyyymm: YearMonth, body: PayrollGenerateBody,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """직종×고용형태 규칙엔진으로 명세 자동생성(P3) → PayrollLine 저장."""
    from erp.hr import payroll_rules
    _scope(user, "hr.payroll", "write")
    emp = await session.get(Employee, body.employee_id)
    if emp is None:
        raise ForbiddenError("존재하지 않는 직원입니다")
    base_pay, dependents = await _resolve_pay_basis(session, emp, body.base_pay, body.dependents)
    from erp.hr.pay_inputs import resolve_monthly_inputs
    from erp.hr.pay_rates import load_rates
    from erp.hr.payroll_settings import PayrollSettingsService
    night_mode = (await PayrollSettingsService().effective(session))["night_pay_mode"]
    auto = await resolve_monthly_inputs(session, emp, yyyymm)
    pick = lambda explicit, key: explicit if explicit is not None else auto[key]  # noqa: E731
    # 입사·퇴사월 역일 일할(2026-10-09 검토 C) — 재직 없는 달은 생성 거부(전액 지급 오류 방지)
    month_first, month_end = _month_bounds(yyyymm)
    wtable, wkids, wnontax, _wfam = await _withholding_inputs(session, [emp.id], yyyymm)
    employed = _employed_days(emp, month_first, month_end)
    if employed <= 0:
        raise ValidationError(
            f"{yyyymm}은 이 직원의 재직 기간(입사 {emp.hired_on}·퇴사 {emp.resigned_on or '-'}) 밖입니다")
    result = payroll_rules.build_payroll(
        staff_class=emp.staff_class, job_category=emp.job_category,
        employment_type=emp.employment_type, base_pay=base_pay,
        daily_wage=float(emp.daily_wage or 0),
        work_days=pick(body.work_days, "work_days"),
        work_hours=pick(body.work_hours, "work_hours"),
        night_hours=pick(body.night_hours, "night_hours"),
        duty_count=pick(body.duty_count, "duty_count"),
        ot_hours=pick(body.ot_hours, "ot_hours"),
        holiday_hours=pick(body.holiday_hours, "holiday_hours"),
        holiday_ot_hours=pick(body.holiday_ot_hours, "holiday_ot_hours"),
        unpaid_days=pick(body.unpaid_days, "unpaid_days"),
        dependents=dependents, year=int(yyyymm[:4]), month=_pay_month(yyyymm), night_pay_mode=night_mode,
        employed_days=employed, month_days=month_end.day,
        rates=await load_rates(session, as_of=date(int(yyyymm[:4]), int(yyyymm.replace("-", "")[4:6]), 1)),
        withholding_table=wtable, children=min(wkids.get(emp.id, 0), max(dependents - 1, 0)), nontax=wnontax)
    record = await _pay.upsert_lines(session, actor=user.user_id,
                                     employee_id=body.employee_id, yyyymm=yyyymm,
                                     lines=result["lines"])
    tot = await _record_totals(session, record.id)
    return {"record_id": record.id, "employment_type": emp.employment_type,
            "job_category": emp.job_category, **tot}


@router.get("/payrolls/{yyyymm}/min-wage")
async def payroll_min_wage(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """최저임금 점검(v1.303) — 그 달 급여 중 미달 건·기준 시급·산입/제외 항목. 막지 않고 드러낸다. 접속기록."""
    from erp.hr import payroll_checks
    scope = _scope(user, "hr.payroll", "read")
    if scope.kind != ScopeKind.ALL:
        raise ForbiddenError("전체 급여 조회 권한이 필요합니다")
    out = await payroll_checks.month_report(session, yyyymm=yyyymm)
    await _audit.record_access(session, actor=user.user_id, action="VIEW", entity="hr.payroll_records",
                               target_id=f"min-wage:{out['yyyymm']}", route="/hr/payrolls/min-wage")
    return out


@router.post("/payrolls/{yyyymm}/generate-bulk")
async def generate_payroll_bulk(yyyymm: YearMonth,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """보수 프로필 기반 재직자 일괄 명세 생성 — 프로필 미등록·일용직·확정분·휴직자 스킵.

    월 변동분(야간·당직·연장)은 근태 집계·듀티에서 자동 산정(P1-②) —
    근태 미수신 직원은 변동분 0으로 생성, 개별 generate로 보정 가능.
    휴직 판정 기준일 = 해당월 1일 (월중 휴직 시작/복직자는 일할계산 개별 처리).

    대상(2026-10-09 전체 흐름 검토 C): 입사일 ≤ 월말 AND (퇴사일 없음 OR 퇴사일 ≥ 월초).
    종전 `resigned_on IS NULL`만 보던 판정은 ①퇴사 예정일이 등록된 재직자를 말없이 누락 ②월말 이후
    입사자를 포함 ③월중 입·퇴사자에게 기본급 전액을 지급했다. 입·퇴사월은 역일 기준 일할
    (기본급 × 재직 역일 ÷ 월 역일·퇴사일=최종 근무일)하고 결과 `prorated`에 근거를 남긴다.
    """
    from erp.hr import payroll_rules
    from erp.hr.models import CompensationProfile
    from erp.hr.pay_inputs import resolve_monthly_inputs_bulk
    from erp.hr.pay_rates import load_rates
    _scope(user, "hr.payroll", "write")
    month_first, month_end = _month_bounds(yyyymm)   # "2026-07" | "202607" 모두 허용
    rates = await load_rates(session, as_of=month_first)   # 급여월 기준 단가(소급 정확성)
    from erp.hr.payroll_settings import PayrollSettingsService
    night_mode = (await PayrollSettingsService().effective(session))["night_pay_mode"]
    rows = (await session.execute(
        select(Employee).where(
            Employee.hired_on <= month_end,
            (Employee.resigned_on.is_(None)) | (Employee.resigned_on >= month_first))
        .order_by(Employee.employee_no))).scalars().all()
    confirmed = {r.employee_id for r in (await session.execute(
        select(PayrollRecord).where(PayrollRecord.yyyymm == yyyymm,
                                    PayrollRecord.confirmed_at.isnot(None)))).scalars().all()}
    on_leave = await _absence.on_leave_employee_ids(session, on=month_first)
    # 점검 M1 — 직원별 순차 조회(N×4) 대신 일괄 로드(쿼리 5회 고정)
    profiles = {p.employee_id: p for p in (await session.scalars(
        select(CompensationProfile).where(
            CompensationProfile.employee_id.in_([e.id for e in rows])))).all()} \
        if rows else {}
    auto_by = await resolve_monthly_inputs_bulk(session, rows, yyyymm)
    wtable, wkids, wnontax, wfam = await _withholding_inputs(session, [e.id for e in rows], yyyymm)
    generated, skipped = [], []
    for emp in rows:
        if emp.id in confirmed:
            skipped.append({"employee_id": emp.id, "reason": "확정 완료"})
            continue
        if emp.id in on_leave:
            skipped.append({"employee_id": emp.id, "reason": "휴직 중 — 복직/일할계산 개별 처리"})
            continue
        if emp.employment_type == "DAILY":
            skipped.append({"employee_id": emp.id, "reason": "일용직 — 근무일수 입력 후 개별 생성"})
            continue
        profile = profiles.get(emp.id)
        if profile is None or float(profile.base_pay) <= 0:
            skipped.append({"employee_id": emp.id, "reason": "보수 프로필 미등록"})
            continue
        auto = auto_by[emp.id]
        employed = _employed_days(emp, month_first, month_end)
        result = payroll_rules.build_payroll(
            staff_class=emp.staff_class, job_category=emp.job_category,
            employment_type=emp.employment_type, base_pay=float(profile.base_pay),
            dependents=wfam.get(emp.id, profile.dependents), year=int(yyyymm[:4]), month=_pay_month(yyyymm), rates=rates,
            night_pay_mode=night_mode, employed_days=employed, month_days=month_end.day,
            withholding_table=wtable, nontax=wnontax,
            children=min(wkids.get(emp.id, 0), max(wfam.get(emp.id, profile.dependents) - 1, 0)),
            **auto)
        record = await _pay.upsert_lines(session, actor=user.user_id,
                                         employee_id=emp.id, yyyymm=yyyymm,
                                         lines=result["lines"])
        tot = await _record_totals(session, record.id)
        item = {"employee_id": emp.id, "record_id": record.id, "gross": tot["gross"], "net": tot["net"]}
        if employed < month_end.day:          # 입·퇴사월 일할 — 검수자가 근거를 볼 수 있게
            item["prorated"] = {"employed_days": employed, "month_days": month_end.day,
                                "hired_on": emp.hired_on, "resigned_on": emp.resigned_on}
        generated.append(item)
    return {"yyyymm": yyyymm, "generated": len(generated), "skipped": skipped,
            "records": generated}


# ── 급여 기초정보(보수 프로필)·온보딩 보완입력 ───────────────
class CompensationBody(BaseModel):
    base_pay: float
    dependents: int = 1
    bank_name: str | None = None
    bank_account: str | None = None   # 저장 시 컬럼 암호화 — 응답은 마스킹만
    effective_from: date | None = None
    note: str | None = None


def _profile_out(profile, masked_account: str | None) -> dict:
    return {"employee_id": profile.employee_id, "base_pay": float(profile.base_pay),
            "dependents": profile.dependents, "bank_name": profile.bank_name,
            "bank_account_masked": masked_account,
            "effective_from": profile.effective_from, "note": profile.note}


@router.get("/compensation/{employee_id}")
async def get_compensation(employee_id: int,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.compensation", "read")
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="hr.compensation_profiles",
                               target_id=str(employee_id), route="/hr/compensation")
    profile = await _comp.get(session, employee_id=employee_id)
    if profile is None:
        return {"employee_id": employee_id, "registered": False}
    return {"registered": True, **_profile_out(profile, _comp.masked_account(profile))}


@router.put("/compensation/{employee_id}")
async def upsert_compensation(employee_id: int, body: CompensationBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """보수 프로필 등록/수정 — HIS/Clinic 동기화 직원의 ERP 보완입력 (온보딩)."""
    _scope(user, "hr.compensation", "write")
    profile = await _comp.upsert(
        session, actor=user.user_id, employee_id=employee_id,
        base_pay=body.base_pay, dependents=body.dependents, bank_name=body.bank_name,
        bank_account=body.bank_account, effective_from=body.effective_from, note=body.note)
    return _profile_out(profile, _comp.masked_account(profile))


@router.get("/onboarding/pending")
async def onboarding_pending(user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """급여 처리 불가(기초정보 누락) 재직자 목록 — 동기화 후 보완입력 체크리스트."""
    _scope(user, "hr.compensation", "read")
    items = await _comp.onboarding_pending(session)
    return {"count": len(items), "items": items}


@router.get("/onboarding/grid")
async def onboarding_grid(only_incomplete: bool = False,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """일괄(스프레드시트) 편집용 그리드 — 재직 직원 전체 + 현재값(기본급·부양가족·은행). complete 플래그.

    only_incomplete=true면 누락자만. 기본은 전원(완료분도 편집)."""
    _scope(user, "hr.compensation", "read")
    items = await _comp.onboarding_grid(session, only_incomplete=only_incomplete)
    return {"count": len(items), "incomplete": sum(1 for i in items if not i["complete"]),
            "items": items}


class BulkCompRow(BaseModel):
    employee_id: int
    base_pay: float = 0
    dependents: int = 1
    bank_name: str | None = None
    bank_account: str | None = None


class BulkCompBody(BaseModel):
    rows: list[BulkCompRow]


@router.post("/onboarding/grid")
async def onboarding_grid_save(body: BulkCompBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """스프레드시트 일괄 저장 — 기본급>0 행만 upsert. 개별 오류는 건너뛰고 결과 집계."""
    _scope(user, "hr.compensation", "write")
    saved, skipped, errors = 0, 0, []
    for r in body.rows:
        if r.base_pay is None or r.base_pay <= 0:
            skipped += 1
            continue
        try:
            async with session.begin_nested():   # savepoint — 한 행 실패가 전체·타행 오염 방지
                await _comp.upsert(session, actor=user.user_id, employee_id=r.employee_id,
                                   base_pay=r.base_pay, dependents=r.dependents,
                                   bank_name=(r.bank_name or None),
                                   bank_account=(r.bank_account or None))
            saved += 1
        except Exception as e:  # noqa: BLE001 — 행 단위 격리
            errors.append({"employee_id": r.employee_id, "error": str(e)[:120]})
    return {"saved": saved, "skipped": skipped, "errors": errors}
class PayRateBody(BaseModel):
    amount: float
    description: str | None = None
    valid_from: date | None = None   # 발효일(기본 오늘) — 연 단위 갱신은 새 발효일로(이력 보존)


@router.get("/pay-rates")
async def list_pay_rates(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """수당 단가 목록 — DB 우선, 미설정 키는 코드 기본값 표시."""
    from erp.hr.pay_rates import list_rates
    _scope(user, "hr.compensation", "read")
    return {"items": await list_rates(session)}


@router.put("/pay-rates/{rate_key}")
async def upsert_pay_rate(rate_key: str, body: PayRateBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """수당 단가 변경 — 단협·취업규칙 개정 반영(코드 수정 불요), 감사기록."""
    from erp.hr.pay_rates import upsert_rate
    _scope(user, "hr.compensation", "write")
    row = await upsert_rate(session, actor=user.user_id, rate_key=rate_key,
                            amount=body.amount, description=body.description,
                            valid_from=body.valid_from)
    return {"rate_key": row.rate_key, "amount": float(row.amount),
            "valid_from": row.valid_from.isoformat()}


@router.get("/pay-rates/{rate_key}/history")
async def pay_rate_history(rate_key: str, user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """수당 단가 변경 이력(발효일 내림차순) — 거버넌스 로그."""
    from erp.hr.pay_rates import rate_history
    _scope(user, "hr.compensation", "read")
    return {"rate_key": rate_key, "items": await rate_history(session, rate_key=rate_key)}


@router.delete("/pay-rates/{rate_key}/{valid_from}")
async def delete_pay_rate(rate_key: str, valid_from: date,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """수당 단가 발효행 삭제 — 오등록(발효일 오타·금액 오등록) 정정. 감사기록."""
    from erp.hr.pay_rates import delete_rate
    _scope(user, "hr.compensation", "write")
    return await delete_rate(session, actor=user.user_id, rate_key=rate_key,
                             valid_from=valid_from)


@router.get("/statutory-params")
async def statutory_params_registry(user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """법정 파라미터 레지스트리(거버넌스) — 4대보험 요율·소득세 파라미터·수당 단가의 등재 연도와
    내년 준비 상태. 내년 미등록은 컴플라이언스 만기(법정 파라미터)로도 경보."""
    from datetime import date as _date

    from erp.hr.pay_rates import list_rates
    from erp.hr.statutory import registered_years
    from erp.hr.statutory_params import registry
    _scope(user, "hr.compensation", "read")
    reg = await registry(session, today=_date.today())
    years = registered_years()
    return {"insurance_rates_years": years["insurance"], "tax_params_years": years["tax"],
            **reg,
            "notes": ["국민연금 기준소득월액 상·하한은 매년 7월 고시 — 연도 요율의 상·하한 확인·갱신(운영 절차)",
                      "미등록 연도 급여·세액 계산은 ValidationError로 차단(fail-safe)",
                      "관리화면 등록값이 코드 상수보다 우선 — 등록 이후 생성하는 급여부터 적용"],
            "pay_rates": await list_rates(session)}


@router.get("/statutory-params/rate-diff")
async def statutory_rate_diff(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """급여 4대보험 공제 ↔ 현재 등록 요율 재계산 차액(읽기 전용·소급 정산 판단용). 보수 담당 권한."""
    from erp.hr.statutory_params import rate_diff
    _scope(user, "hr.compensation", "write")
    out = await rate_diff(session, yyyymm=yyyymm)
    # 전 직원 사번·보수월액 열람 — 다른 급여 조회와 같이 접속기록(개인정보 안전성 확보조치 §8)
    await _audit.record_access(session, actor=user.user_id, action="VIEW", entity="hr.payroll_records",
                               target_id=f"rate-diff:{out['yyyymm']}",
                               route="/hr/statutory-params/rate-diff")
    return out


class WithholdingActivateBody(BaseModel):
    source_note: str


@router.get("/withholding-tables")
async def withholding_tables(user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """근로소득 간이세액표 목록 — 후보/활성/폐기·대조용 표본 칸(v1.297.0)."""
    from erp.hr import withholding_table as wht
    _scope(user, "hr.compensation", "write")
    return {"items": await wht.list_tables(session)}


@router.post("/withholding-tables/{table_id}/activate")
async def withholding_table_activate(table_id: int, body: WithholdingActivateBody,
                                     user: CurrentUser = Depends(get_current_user),
                                     session: AsyncSession = Depends(get_session)):
    """후보 표 활성화 — 홈택스 원표 대조 근거 필수·감사. 이후 생성하는 급여부터 표로 계산."""
    from erp.hr import withholding_table as wht
    _scope(user, "hr.compensation", "write")
    out = await wht.activate(session, actor=user.user_id, table_id=table_id, source_note=body.source_note)
    await session.commit()
    return out


class WithholdingDiscardBody(BaseModel):
    reason: str


@router.post("/withholding-tables/{table_id}/discard")
async def withholding_table_discard(table_id: int, body: WithholdingDiscardBody,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """후보 폐기 — 원표 대조에서 틀린 후보를 RETIRED로(사유 필수·감사). 계산에는 영향 없음(후보는 원래 안 쓴다)."""
    from erp.hr import withholding_table as wht
    _scope(user, "hr.compensation", "write")
    out = await wht.discard(session, actor=user.user_id, table_id=table_id, reason=body.reason)
    await session.commit()
    return out


class WithholdingRetaxBody(BaseModel):
    note: str


@router.get("/withholding-tables/{table_id}/retax-preview")
async def withholding_table_retax_preview(table_id: int, user: CurrentUser = Depends(get_current_user),
                                          session: AsyncSession = Depends(get_session)):
    """활성화 전에 만든 미확정 급여의 소득세 재계산 미리보기(읽기 전용) — 직원별 기록↔표 세액. 접속기록."""
    from erp.hr import withholding_table as wht
    _scope(user, "hr.compensation", "write")
    out = await wht.retax_preview(session, table_id=table_id)
    await _audit.record_access(session, actor=user.user_id, action="VIEW", entity="hr.payroll_records",
                               target_id=f"withholding-retax:{table_id}", route="/hr/withholding-tables/retax-preview")
    return out


@router.post("/withholding-tables/{table_id}/retax")
async def withholding_table_retax(table_id: int, body: WithholdingRetaxBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """미확정 급여 소득세·지방소득세 줄만 활성 표로 재계산 — 확정분·다른 줄은 그대로. 사유 필수·감사."""
    from erp.hr import withholding_table as wht
    _scope(user, "hr.compensation", "write")
    _scope(user, "hr.payroll", "write")
    out = await wht.retax(session, actor=user.user_id, table_id=table_id, note=body.note)
    await session.commit()
    return out


@router.get("/withholding-tables/{table_id}/impact")
async def withholding_table_impact(table_id: int, yyyymm: YearMonth,
                                   user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """영향 미리보기(읽기 전용) — 그 달 급여의 기록 소득세 ↔ 이 표 세액(직원별). 접속기록."""
    from erp.hr import withholding_table as wht
    _scope(user, "hr.compensation", "write")
    out = await wht.impact(session, table_id=table_id, yyyymm=yyyymm)
    await _audit.record_access(session, actor=user.user_id, action="VIEW", entity="hr.payroll_records",
                               target_id=f"withholding-impact:{out['yyyymm']}",
                               route="/hr/withholding-tables/impact")
    return out


class RateSettlementBody(BaseModel):
    source_yyyymm: YearMonth
    method: Literal["NEXT_PAYROLL_ADJUST", "EMPLOYER_ABSORB", "NOT_PAYROLL_SOT"]
    target_yyyymm: YearMonth | None = None
    note: str


class RateSettlementCancelBody(BaseModel):
    reason: str


@router.get("/statutory-params/rate-settlement")
async def rate_settlement_overview(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """요율 차액 정산 현황 — 실행 이력·연결된 결정(결정 대기함)."""
    from erp.hr import rate_settlement
    _scope(user, "hr.compensation", "write")
    return await rate_settlement.overview(session, source=yyyymm)


@router.get("/statutory-params/rate-settlement/preview")
async def rate_settlement_preview(yyyymm: YearMonth,
                                  method: Literal["NEXT_PAYROLL_ADJUST", "EMPLOYER_ABSORB", "NOT_PAYROLL_SOT"],
                                  target_yyyymm: YearMonth | None = None,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """방식별 실행 계획(쓰기 없음) — 직원별 처분(다음 급여 공제/기관 부담/기록)·전표 금액·막는 사유."""
    from erp.hr import rate_settlement
    _scope(user, "hr.compensation", "write")
    out = await rate_settlement.preview(session, source=yyyymm, method=method, target=target_yyyymm)
    await _audit.record_access(session, actor=user.user_id, action="VIEW", entity="hr.payroll_records",
                               target_id=f"rate-settlement:{yyyymm}",
                               route="/hr/statutory-params/rate-settlement/preview")
    return out


@router.post("/statutory-params/rate-settlement", status_code=201)
async def rate_settlement_execute(body: RateSettlementBody, user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """정산 방식 실행 + 결정 기록(한 트랜잭션) — 급여 공제 줄·전표·결정 대기함이 함께 바뀐다."""
    from erp.hr import rate_settlement
    _scope(user, "hr.compensation", "write")
    _scope(user, "hr.payroll", "write")
    out = await rate_settlement.execute(session, actor=user.user_id, source=body.source_yyyymm,
                                        method=body.method, target=body.target_yyyymm, note=body.note)
    await session.commit()
    return out


@router.post("/statutory-params/rate-settlement/{settlement_id}/absorb-remaining")
async def rate_settlement_absorb_remaining(settlement_id: int, body: RateSettlementCancelBody,
                                           user: CurrentUser = Depends(get_current_user),
                                           session: AsyncSession = Depends(get_session)):
    """끝내 공제되지 못한 직원분(실행 후 퇴사·휴직·미생성)을 기관 부담 전표로 — 미회수 방치 방지."""
    from erp.hr import rate_settlement
    _scope(user, "hr.compensation", "write")
    _scope(user, "hr.payroll", "write")
    out = await rate_settlement.absorb_remaining(session, actor=user.user_id, settlement_id=settlement_id,
                                                 reason=body.reason)
    await session.commit()
    return out


@router.post("/statutory-params/rate-settlement/{settlement_id}/cancel")
async def rate_settlement_cancel(settlement_id: int, body: RateSettlementCancelBody,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """정산 취소 — 공제 확정분이 없을 때만. 전표 역분개·공제 줄 제거·결정 재오픈."""
    from erp.hr import rate_settlement
    _scope(user, "hr.compensation", "write")
    _scope(user, "hr.payroll", "write")
    out = await rate_settlement.cancel(session, actor=user.user_id, settlement_id=settlement_id,
                                       reason=body.reason)
    await session.commit()
    return out


class StatutoryInsuranceBody(BaseModel):
    values: dict
    source_note: str


class StatutoryTaxCarryBody(BaseModel):
    from_year: int
    source_note: str


@router.put("/statutory-params/{year}/insurance")
async def put_statutory_insurance(year: int, body: StatutoryInsuranceBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """연도 4대보험 근로자 요율 등록(고시 근거 필수) — 코드 상수보다 우선·감사 기록."""
    from erp.hr.statutory_params import set_insurance, unconfirmed_payrolls
    _scope(user, "hr.compensation", "write")
    row = await set_insurance(session, year=year, values=body.values,
                              source_note=body.source_note, actor=user.user_id)
    out = {"year": row.year, "values": row.values, "source_note": row.source_note,
           "unconfirmed_payrolls": await unconfirmed_payrolls(session, year=year)}
    await _commit_then_refresh(session)
    return out


@router.post("/statutory-params/{year}/tax-carry")
async def post_statutory_tax_carry(year: int, body: StatutoryTaxCarryBody,
                                   user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """세법 개정 없음 확인 → 전년 소득세 파라미터 승계(사람 결정·근거 필수·감사)."""
    from erp.hr.statutory_params import carry_tax
    _scope(user, "hr.compensation", "write")
    row = await carry_tax(session, year=year, from_year=body.from_year,
                          source_note=body.source_note, actor=user.user_id)
    out = {"year": row.year, "from_year": row.values["from_year"], "source_note": row.source_note}
    await _commit_then_refresh(session)
    return out


class StatutoryTaxAmendBody(BaseModel):
    from_year: int
    overrides: dict                       # {항목 키: 값} — 바뀐 항목만(구간표는 행 목록·마지막 상한 null)
    source_note: str


@router.get("/statutory-params/tax/{year}")
async def get_statutory_tax_params(year: int, user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """그 해 소득세 파라미터 전체(JSON 모양) — '개정 반영' 화면 프리필용. 미등록 연도는 422."""
    from erp.hr import statutory
    _scope(user, "hr.compensation", "read")
    await statutory.refresh_db_params(session)
    return {"year": year, "params": statutory.tax_params_json(year)}


@router.post("/statutory-params/{year}/tax-amend")
async def post_statutory_tax_amend(year: int, body: StatutoryTaxAmendBody,
                                   user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """세법 개정 반영 — 원본 연도 위에 바뀐 항목만 등록(코드 배포 없이·근거 필수·감사). 같은 연도 승계는 대체된다."""
    from erp.hr.statutory_params import amend_tax
    _scope(user, "hr.compensation", "write")
    row = await amend_tax(session, year=year, from_year=body.from_year, overrides=body.overrides,
                          source_note=body.source_note, actor=user.user_id)
    out = {"year": row.year, "from_year": row.values["from_year"],
           "overrides": sorted(row.values["overrides"]), "source_note": row.source_note}
    await _commit_then_refresh(session)
    return out


async def _commit_then_refresh(session) -> None:
    """커밋이 성공한 뒤에만 계산 캐시에 반영(G7 — 커밋 실패 시 DB에 없는 요율로 계산하던 창 제거).

    ⚠ 커밋 뒤에는 이 세션으로 DB를 다시 건드리지 않는다 — 실제 get_session은 `session.begin()`
    컨텍스트 안이라 커밋 후 SELECT가 InvalidRequestError(500)를 냈다(2차 검토 H4·저장은 되고 응답만
    실패). 캐시를 무효화해 다음 요청의 세션 훅이 새로 읽게 한다.
    """
    from erp.hr import statutory
    await session.commit()
    statutory.invalidate_cache()


# ── 노동규제 룰 관리 (FR-32) — 시프트 카탈로그·임계값 ─────────
class ShiftTypeBody(BaseModel):
    label: str
    work_hours: float
    is_night: bool = False
    counts_as_workday: bool = True
    active: bool = True
    start_time: str | None = None   # "HH:MM" — 근태 지각/조퇴 판정 기준(선택)
    end_time: str | None = None


class LaborRuleBody(BaseModel):
    threshold: float
    notice_ref: str | None = None
    active: bool = True


@router.get("/shift-types")
async def list_shift_types(user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """시프트 카탈로그 — 코드 기본값(D/E/N/OFF) 위에 등록 시프트 오버레이한 유효 뷰."""
    from erp.hr.labor_catalog import list_shift_types as _list
    _scope(user, "hr.duties", "read")
    return {"items": await _list(session)}


@router.put("/shift-types/{code}")
async def upsert_shift_type(code: str, body: ShiftTypeBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """시프트 등록·수정 — 당직(OC 24h)·온콜 등 장시간 시프트 등록 시 80h 룰 실효화. 감사기록."""
    from erp.hr.labor_catalog import upsert_shift_type as _upsert
    _scope(user, "hr.duties", "write")
    row = await _upsert(session, actor=user.user_id, code=code, label=body.label,
                        work_hours=body.work_hours, is_night=body.is_night,
                        counts_as_workday=body.counts_as_workday, active=body.active,
                        start_time=body.start_time, end_time=body.end_time)
    return {"code": row.code, "work_hours": float(row.work_hours)}


@router.get("/labor-rules")
async def list_labor_rules(user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """노동규제 임계값 — 법 조문 기본값 위에 인사팀 확정값 오버레이한 유효 뷰."""
    from erp.hr.labor_catalog import list_labor_rules as _list
    _scope(user, "hr.duties", "read")
    return {"items": await _list(session)}


@router.put("/labor-rules/{rule_key}")
async def upsert_labor_rule(rule_key: str, body: LaborRuleBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """임계값 변경 — 법 개정·기관 정책 반영(코드 수정 불요). 듀티 편성 경고에 즉시 반영. 감사기록."""
    from erp.hr.labor_catalog import upsert_labor_rule as _upsert
    _scope(user, "hr.duties", "write")
    row = await _upsert(session, actor=user.user_id, rule_key=rule_key,
                        threshold=body.threshold, notice_ref=body.notice_ref,
                        active=body.active)
    return {"rule_key": row.rule_key, "threshold": float(row.threshold)}


# ── 부양가족 마스터 (P3) ─────────────────────────────────
class DependentBody(BaseModel):
    name: str
    relation: str          # SPOUSE|CHILD|PARENT|GRANDPARENT|SIBLING|OTHER
    birth_date: date | None = None
    disabled: bool = False
    counted: bool | None = None   # 미지정 시 연령 기준 자동 제안
    note: str | None = None


@router.get("/employees/{employee_id}/dependents")
async def list_dependents(employee_id: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    from erp.hr.dependent_service import DependentService
    _scope(user, "hr.compensation", "read")
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="hr.dependents", target_id=str(employee_id),
                               route="/hr/employees/dependents")
    svc = DependentService()
    items = await svc.list(session, employee_id=employee_id)
    return {"items": items,
            "payroll_dependents": await svc.payroll_dependents(session, employee_id)}


@router.post("/employees/{employee_id}/dependents", status_code=201)
async def add_dependent(employee_id: int, body: DependentBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """부양가족 등록 — counted 미지정 시 연령 기준 자동 제안(담당자 수정 가능)."""
    from erp.hr.dependent_service import DependentService
    _scope(user, "hr.compensation", "write")
    d = await DependentService().add(session, actor=user.user_id,
                                     employee_id=employee_id, **body.model_dump())
    return {"id": d.id, "counted": d.counted}


@router.delete("/dependents/{dependent_id}")
async def remove_dependent(dependent_id: int,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    from erp.hr.dependent_service import DependentService
    _scope(user, "hr.compensation", "write")
    await DependentService().remove(session, actor=user.user_id,
                                    dependent_id=dependent_id)
    return {"deleted": True}


# ── 휴직/복직·퇴사 (P1-①) ────────────────────────────────
class AbsenceStartBody(BaseModel):
    employee_id: int
    kind: Literal["MATERNITY", "PARENTAL", "SICK", "INDUSTRIAL", "PERSONAL"]
    start_on: date
    expected_return_on: date | None = None
    insurance_exempt: bool = True    # 4대보험 납부예외 신청 대상 여부
    note: str | None = None


@router.post("/absences", status_code=201)
async def start_absence(body: AbsenceStartBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """휴직 등록 — 발령(LEAVE) 기록, 급여 일괄생성 제외·납부예외 식별."""
    _scope(user, "hr.employees", "write")
    a = await _absence.start(session, actor=user.user_id, **body.model_dump())
    return {"id": a.id, "kind": a.kind, "start_on": a.start_on}


@router.post("/absences/{absence_id}/return")
async def return_from_absence(absence_id: int, return_on: date,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """복직 처리 — 발령(REINSTATE) 기록."""
    _scope(user, "hr.employees", "write")
    a = await _absence.end(session, actor=user.user_id,
                           absence_id=absence_id, return_on=return_on)
    return {"id": a.id, "return_on": a.return_on}


@router.get("/absences")
async def list_absences(active: bool = False,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    scope = _scope(user, "hr.employees", "read")   # 행 scope 합성(§6.2) — 전 직원 휴직명단 노출 차단
    if scope.kind == ScopeKind.SELF:
        self_id = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
        if self_id is None:
            return {"count": 0, "items": []}
        items = await _absence.list(session, active_only=active, employee_id=self_id)
    elif scope.kind == ScopeKind.DEPT:
        items = await _absence.list(session, active_only=active, dept_id=user.dept_id)
    else:
        items = await _absence.list(session, active_only=active)
    return {"count": len(items), "items": items}


# ── 법정의무교육·건강검진 (P3) ───────────────────────────
class ComplianceBody(BaseModel):
    employee_id: int
    category: str          # SEXUAL_HARASSMENT|PRIVACY|SAFETY|DISABILITY|CHECKUP_GENERAL|CHECKUP_SPECIAL
    done_on: date
    note: str | None = None


@router.post("/compliance", status_code=201)
async def record_compliance(body: ComplianceBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """이수 기록 — 주기별 만료 자동 산정, (직원·항목·이수일) 멱등."""
    from erp.hr.compliance_service import ComplianceService
    _scope(user, "hr.employees", "write")
    rec = await ComplianceService().record(session, actor=user.user_id,
                                           **body.model_dump())
    return {"id": rec.id, "valid_until": rec.valid_until}


@router.get("/compliance/status")
async def compliance_status(user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """재직자 × 필수항목 매트릭스(OK/임박/만료/미이수) + 이수율 — 인증평가 대비."""
    from erp.hr.compliance_service import ComplianceService
    _scope(user, "hr.compensation", "read")
    return await ComplianceService().status(session)


@router.get("/compliance/status.xlsx")
async def compliance_status_xlsx(user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    from fastapi.responses import StreamingResponse

    from erp.common.excel import build_xlsx
    from erp.hr.compliance_service import ComplianceService
    _scope(user, "hr.compensation", "read")
    out = await ComplianceService().status(session)
    cats = [c["key"] for c in out["categories"]]
    labels = [c["label"] for c in out["categories"]]
    rows = [[i["employee_no"], i["name"], i["job_category"] or ""] +
            [i["cells"][c]["state"] for c in cats] for i in out["items"]]
    buf = build_xlsx(sheet_name="법정교육·검진 현황",
                     headers=["사번", "이름", "직종"] + labels, rows=rows)
    return StreamingResponse(
        iter([buf]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="compliance-status.xlsx"'})


# ── 4대보험 취득·상실 신고 추적 (P1-③) ──────────────────
@router.get("/insurance-reports")
async def insurance_reports(status: str = "PENDING",
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """신고 이벤트 목록 — 입사(취득)·퇴사(상실)·휴직(납부예외)·복직(재개), 기한·연체 표시."""
    from erp.hr.insurance_report_service import InsuranceReportService
    _scope(user, "hr.compensation", "read")
    items = await InsuranceReportService().pending(session, status=status,
                                                   today=date.today())
    return {"count": len(items), "items": items}


@router.post("/insurance-reports/{event_id}/reported")
async def mark_insurance_reported(event_id: int, reported_on: date,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """공단 신고 완료 처리."""
    from erp.hr.insurance_report_service import InsuranceReportService
    _scope(user, "hr.compensation", "write")
    ev = await InsuranceReportService().mark_reported(
        session, actor=user.user_id, event_id=event_id, reported_on=reported_on)
    return {"id": ev.id, "status": ev.status, "reported_on": ev.reported_on}


class ResignBody(BaseModel):
    resigned_on: date
    note: str | None = None
    reason: str | None = None    # 자발-이직·자발-개인사정·비자발-계약만료·권고사직·정년 등


@router.post("/employees/{employee_id}/resign")
async def resign_employee(employee_id: int, body: ResignBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """퇴사 처리 — resigned_on 확정 + 발령(RESIGN). HIS 관리 직원은 HIS에서(SoT)."""
    _scope(user, "hr.employees", "write")
    emp = await _absence.resign(session, actor=user.user_id, employee_id=employee_id,
                                resigned_on=body.resigned_on, note=body.note, reason=body.reason)
    return {"employee_id": emp.id, "resigned_on": emp.resigned_on, "resign_reason": emp.resign_reason}


# ── 퇴직 정산(오프보딩) (P2-①) ──────────────────────────
@router.get("/employees/{employee_id}/offboarding")
async def offboarding_preview(employee_id: int,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """퇴직 정산 미리보기 — 연차수당·퇴직금·충당 상계 내역."""
    from erp.hr.offboarding_service import OffboardingService
    _scope(user, "hr.compensation", "read")
    return await OffboardingService().preview(session, employee_id=employee_id)


@router.post("/employees/{employee_id}/offboarding")
async def offboarding_settle(employee_id: int,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """퇴직 정산 확정 — 전표 생성(충당상계+비용/미지급) + 정산 기록(1회 멱등)."""
    from erp.hr.offboarding_service import OffboardingService
    _scope(user, "hr.payroll", "write")
    return await OffboardingService().settle(session, actor=user.user_id,
                                             employee_id=employee_id)


@router.post("/payrolls/{yyyymm}/confirm")
async def confirm_payroll(yyyymm: YearMonth, employee_id: int, paid_on: date | None = None,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.payroll", "write")
    record = await _pay.confirm(session, actor=user.user_id,
                                employee_id=employee_id, yyyymm=yyyymm, paid_on=paid_on)
    return {"record_id": record.id, "confirmed_at": record.confirmed_at,
            "paid_on": record.paid_on}


@router.get("/payrolls/{yyyymm}/reconcile")
async def payroll_reconcile(yyyymm: YearMonth, employee_id: int,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """급여 명세 ↔ GL 전표 대사(H3) — 지급/공제 합계 vs 급여+성과급 전표·근로자부담 예수금."""
    scope = _scope(user, "hr.payroll", "read")
    await _assert_employee_access(session, user, scope, employee_id)
    record = (await session.execute(
        select(PayrollRecord).where(PayrollRecord.employee_id == employee_id,
                                    PayrollRecord.yyyymm == yyyymm))).scalar_one_or_none()
    if record is None:
        raise ValidationError("급여 데이터가 없습니다")
    return await _pay.reconcile(session, record=record)


async def _item_catalog(session: AsyncSession) -> dict:
    """항목코드→카탈로그(한글명·과세·표시순) — 급여명세 표기 SoT(P0)."""
    from erp.hr.models import PayItemCatalog
    return {c.code: c for c in (await session.scalars(select(PayItemCatalog))).all()}


def _payslip_rows(lines, cat: dict, kind: str) -> list[dict]:
    """kind(EARNING/DEDUCTION) 라인 → 한글명·과세 부여·표시순 정렬. 미등록 코드는 원본코드 폴백."""
    rows = []
    for ln in lines:
        if ln.item_kind != kind:
            continue
        c = cat.get(ln.item_code)
        rows.append({"item_code": ln.item_code,
                     "item_name": (c.name if c else ln.item_code),
                     "amount": float(ln.amount),
                     "taxable": (c.taxable if c else True),
                     "formula": (ln.calc_basis or {}).get("formula"),  # 계산방법(§27의2 5호)
                     "_order": (c.display_order if c else 100)})
    rows.sort(key=lambda r: (r["_order"], r["item_code"]))
    for r in rows:
        del r["_order"]
    return rows


async def _incentive_detail(session: AsyncSession, emp_id: int, yyyymm: str) -> dict | None:
    """의료진 성과급 실적 배분 근거(P2) — 승인된 IncentiveCalculation.snapshot 재현(재계산 아님).

    본인 명세 경로에서만 호출 — 진료실적은 본인만 노출(M7). 일괄발행(관리자)에는 실적 섹션 미포함.
    """
    from erp.hr.models import IncentiveCalculation
    ic = await session.scalar(select(IncentiveCalculation).where(
        IncentiveCalculation.employee_id == emp_id,
        IncentiveCalculation.period == yyyymm,
        IncentiveCalculation.status == "APPROVED"))
    if ic is None:
        return None
    snap = ic.snapshot or {}
    net = snap.get("net_revenue")
    net = float(net) if net is not None else float(ic.net_revenue)
    rate = snap.get("rate_pct")
    return {"incentive_type": snap.get("incentive_type"),
            "rate_pct": rate,
            "revenue": (float(snap["revenue"]) if snap.get("revenue") is not None else None),
            "net_revenue": net, "amount": float(ic.amount),
            "formula": (f"순수입 {int(net):,} × {rate:g}%" if rate is not None else None)}


@router.get("/payrolls/me/{yyyymm}")
async def my_payroll(yyyymm: YearMonth,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """본인 급여명세 조회 (FR-07b) — 확정분만, 접속기록. 항목 한글명(카탈로그) 병기."""
    emp_id = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
    if emp_id is None:
        raise ForbiddenError()
    record = (await session.execute(
        select(PayrollRecord).where(PayrollRecord.employee_id == emp_id,
                                    PayrollRecord.yyyymm == yyyymm,
                                    PayrollRecord.confirmed_at.isnot(None))
    )).scalar_one_or_none()
    if record is None:
        return {"yyyymm": yyyymm, "confirmed": False, "lines": []}
    lines = (await session.execute(
        select(PayrollLine).where(PayrollLine.record_id == record.id))).scalars().all()
    cat = await _item_catalog(session)
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="hr.payroll_records", target_id=str(record.id),
                               route=f"/hr/payrolls/me/{yyyymm}")
    # 계산방법(§27의2 5호)·과세구분은 PDF에만 있고 JSON엔 없어 화면에서 확인할 수 없었다 —
    # 명세서 법정 기재사항이므로 동일하게 내보낸다(_payslip_rows와 같은 규약).
    return {"yyyymm": yyyymm, "confirmed": True, "paid_on": record.paid_on,
            "lines": [{"item_code": ln.item_code, "item_kind": ln.item_kind,
                       "item_name": (cat[ln.item_code].name if ln.item_code in cat
                                     else ln.item_code),
                       "taxable": (cat[ln.item_code].taxable if ln.item_code in cat else True),
                       "formula": (ln.calc_basis or {}).get("formula"),
                       "amount": float(ln.amount)} for ln in lines]}


# ── 급여 법정처리 (FR-07 확장): 4대보험·퇴직금·연말정산 ───────
@router.get("/payrolls/{yyyymm}/insurance")
async def payroll_insurance(yyyymm: YearMonth, employee_id: int,
                            work_days: int | None = None, work_hours: float | None = None,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """4대보험 근로자 부담분 산정 (보수월액 기준) — 고용형태 특례(일용 적용기준) 반영."""
    from erp.hr import statutory
    scope = _scope(user, "hr.payroll", "read")
    await _assert_employee_access(session, user, scope, employee_id)
    emp = await session.get(Employee, employee_id)
    if emp is None:
        raise ForbiddenError("존재하지 않는 직원입니다")
    from erp.hr.employee_service import _EXTERNAL_LINE_CODES
    from erp.hr.payroll_rules import load_nontax, nontax_exempt
    month_lines = (await session.execute(
        select(PayrollLine.item_code, PayrollLine.item_kind, PayrollLine.amount)
        .join(PayrollRecord, PayrollRecord.id == PayrollLine.record_id)
        .where(PayrollRecord.employee_id == employee_id, PayrollRecord.yyyymm == yyyymm,
               PayrollLine.item_kind == "EARNING",
               PayrollLine.item_code.notin_(_EXTERNAL_LINE_CODES)))).all()   # 성과급 등 외부 줄 제외(공제 줄과 같은 기준)
    lines = [{"item_code": c, "item_kind": k, "amount": float(a)} for c, k, a in month_lines]
    # 보수월액 = 지급 합계 − 비과세(한도까지) — 생성된 공제 줄과 같은 기준(v1.303)
    salary = sum(x["amount"] for x in lines) - nontax_exempt(lines, await load_nontax(session))
    return {"employee_id": employee_id, "yyyymm": yyyymm, "보수월액": salary,
            "employment_type": emp.employment_type,
            **statutory.insurance(salary, year=int(yyyymm[:4]), month=_pay_month(yyyymm),
                                  employment_type=emp.employment_type,
                                  work_days=work_days, work_hours=work_hours)}


@router.get("/employees/{employee_id}/daily-withholding")
async def daily_withholding(employee_id: int, days: int, daily_wage: float | None = None,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """일용근로 소득세 원천징수(분리과세) — 일급 미지정 시 직원 daily_wage 사용."""
    from erp.hr import statutory
    scope = _scope(user, "hr.payroll", "read")
    await _assert_employee_access(session, user, scope, employee_id)
    emp = await session.get(Employee, employee_id)
    if emp is None:
        raise ForbiddenError("존재하지 않는 직원입니다")
    wage = daily_wage if daily_wage is not None else float(emp.daily_wage or 0)
    if wage <= 0:
        raise ValidationError("일급(daily_wage)이 없습니다 — 일용직만 해당")
    return {"employee_id": employee_id, "employment_type": emp.employment_type,
            **statutory.daily_withholding(wage, days=days)}


@router.get("/employees/{employee_id}/severance")
async def employee_severance(employee_id: int, as_of: date,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """퇴직금 추정 — 최근 급여 기준 평균임금 × 근속(일할)."""
    from erp.hr import statutory
    scope = _scope(user, "hr.employees", "read")
    await _assert_employee_access(session, user, scope, employee_id)
    emp = await session.get(Employee, employee_id)
    if emp is None:
        raise ForbiddenError("존재하지 않는 직원입니다")
    # 최근 1개월 EARNING이던 종전 산식은 일할된 달·변동수당 달에 왜곡 — 법정 평균임금으로(시나리오 S-C)
    from erp.hr.retirement_service import statutory_avg_wage
    anchor = min(as_of, emp.resigned_on) if emp.resigned_on else as_of
    avg = await statutory_avg_wage(session, emp, anchor=anchor)
    # 퇴직금은 반올림 전 값으로 — 정산(offboarding)과 1원 어긋나지 않게(시나리오 2회차)
    return {"employee_id": employee_id, "avg_monthly_wage": round(avg),
            **statutory.severance(avg, hire_date=emp.hired_on, as_of=as_of)}


@router.get("/year-end/{year}/readiness")
async def year_end_readiness(year: int, user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """연말정산 준비(v1.304) — 귀속 연도 전 직원 연간 집계·간이 정산 미리보기·확인할 점·합계. 전체 범위만·접속기록."""
    from erp.hr import year_end
    scope = _scope(user, "hr.payroll", "read")
    if scope.kind != ScopeKind.ALL:
        raise ForbiddenError("전체 급여 조회 권한이 필요합니다")
    if not 2020 <= year <= 2100:
        raise ValidationError("연도가 올바르지 않습니다")
    out = await year_end.readiness(session, year=year)
    await _audit.record_access(session, actor=user.user_id, action="VIEW", entity="hr.payroll_records",
                               target_id=f"year-end-readiness:{year}", route="/hr/year-end/readiness")
    return out


@router.get("/year-end/{year}/readiness.xlsx")
async def year_end_readiness_xlsx(year: int, user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """연말정산 준비 엑셀 — 외부 정산(세무 프로그램·홈택스)에 넘길 직원별 연간 총급여·기납부세액·보험료. 다운로드 감사(EXPORT)."""
    from fastapi.responses import StreamingResponse

    from erp.common.excel import build_xlsx
    from erp.hr import year_end
    scope = _scope(user, "hr.payroll", "read")
    if scope.kind != ScopeKind.ALL:
        raise ForbiddenError("전체 급여 조회 권한이 필요합니다")
    if not 2020 <= year <= 2100:
        raise ValidationError("연도가 올바르지 않습니다")
    out = await year_end.readiness(session, year=year)
    headers = ["사번", "이름", "총급여(비과세 제외)", "비과세", "기납부 소득세", "기납부 지방소득세", "국민연금", "건강·장기요양·고용",
               "확정 개월", "공제대상가족", "8~20세 자녀", "간이 결정세액", "간이 정산액(+환급)", "공제 방식", "확인할 점"]
    rows = [[r["employee_no"], r["name"], r["taxable"], r["nontax"], r["income_tax"], r["local_tax"], r["pension"],
             r["insurance"], r["months"], r["family"], r["children"], r["determined"], r["settlement"], r["credit_basis"],
             " · ".join(r["flags"])]
            for r in out["rows"]]
    await _audit.record_access(session, actor=user.user_id, action="EXPORT", entity="hr.payroll_records",
                               target_id=f"year-end-readiness:{year}", route="/hr/year-end/readiness.xlsx")
    data = build_xlsx(sheet_name=f"{year} 연말정산 준비", headers=headers, rows=rows)
    import io
    return StreamingResponse(io.BytesIO(data), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="year-end-{year}.xlsx"'})


@router.get("/employees/{employee_id}/year-end")
async def employee_year_end(employee_id: int, year: int, dependents: int | None = None, children: int | None = None,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """연말정산(간이) — 연간 총급여(비과세 제외)·기납부세액·납부 보험료 기준. 원천징수 정산 명세(직원 신원·마스킹 주민번호 포함).

    집계는 연말정산 준비 화면과 같은 단일 소스(`year_end.annual_totals` — ESS 원천징수영수증 기준: 확정 급여 + 성과급 외부 줄 + 퇴사
    연차수당). 가족 수·자녀를 안 넘기면 급여 생성과 같은 기준(부양가족 등록 > 보수 프로필). 주민번호는 마스킹만."""
    from erp.hr import statutory, year_end
    from erp.hr.compensation_service import CompensationService
    from erp.hr.models import Dependent
    from erp.hr.withholding_table import children_8_20, family_from
    scope = _scope(user, "hr.payroll", "read")
    await _assert_employee_access(session, user, scope, employee_id)
    emp = await session.get(Employee, employee_id)
    prof = await CompensationService().get(session, employee_id=employee_id)
    masked = CompensationService().masked_resident(prof) if prof is not None else None
    deps = (await session.scalars(select(Dependent).where(Dependent.employee_id == employee_id))).all()
    dep_source = "입력"
    if dependents is None:
        dependents = family_from(deps, getattr(prof, "dependents", None))
        dep_source = "부양가족 등록" if deps else ("보수 프로필" if prof is not None else "기본값(1)")
    if children is None:
        children = children_8_20(deps, int(year))
    children = min(max(int(children), 0), max(dependents - 1, 0))     # 입력값도 가족−1 이하(본인 제외)
    t = (await year_end.annual_totals(session, year=int(year), employee_ids=[employee_id])).get(employee_id) or {
        "gross": 0.0, "taxable": 0.0, "nontax": 0.0, "income_tax": 0.0, "pension": 0.0, "insurance": 0.0,
        "unconfirmed_months": [], "approx_months": []}
    return {"employee_id": employee_id, "employee_name": emp.name if emp else None,
            "employee_no": emp.employee_no if emp else None, "resident_masked": masked,
            "year": year, "dependents": dependents, "dependents_source": dep_source, "children": children,
            "unconfirmed_months": len(t["unconfirmed_months"]), "approx_months": len(t["approx_months"]),
            "nontax": round(t["nontax"]),
            "basis_note": "확정 급여(+성과급·퇴사 연차수당) 합산 — ESS 원천징수영수증과 같은 기준 · 총급여는 비과세 제외 · 간이 산식(근로소득공제·"
                          "인적공제·연금보험료·건강·장기요양·고용보험료·근로소득세액공제·자녀) — 의료비·교육비·기부금·카드 등 증빙 공제 미반영이라 "
                          "결정세액이 실제보다 클 수 있습니다",
            **statutory.year_end_settlement(t["taxable"], withheld_tax=t["income_tax"], year=int(year),
                                            dependents=dependents, children=children,
                                            pension_paid=t["pension"], insurance_paid=t["insurance"])}


# ── 근태 (FR-29) · 증명서 (FR-23) ──────────────────────────
@router.get("/attendance")
async def attendance(yyyymm: YearMonth, employee_id: int | None = None,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    scope = _scope(user, "hr.employees", "read")
    q = select(AttendanceSummary).where(AttendanceSummary.yyyymm == yyyymm)
    if scope.kind == ScopeKind.SELF:
        emp_id = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
        q = q.where(AttendanceSummary.employee_id == emp_id)
    elif scope.kind == ScopeKind.DEPT:
        q = q.where(AttendanceSummary.employee_id.in_(
            select(Employee.id).where(Employee.department_id == scope.dept_id)))
    elif employee_id is not None:
        q = q.where(AttendanceSummary.employee_id == employee_id)
    rows = (await session.execute(q)).scalars().all()
    # 이름·사번 병기 — 지각·조퇴·야간 횟수는 개인별 조치 판단값인데 내부 PK로는 대상자를 특정할 수
    # 없었다. 행 스코프(SELF/DEPT)는 위에서 이미 강제되므로 노출 범위는 그대로다.
    from erp.hr.employee_service import employee_labels
    labels = await employee_labels(session, [r.employee_id for r in rows])
    return {"items": [{"employee_id": r.employee_id,
                       "employee_name": labels.get(r.employee_id, {}).get("name"),
                       "employee_no": labels.get(r.employee_id, {}).get("employee_no"),
                       "work_days": r.work_days,
                       "late_count": r.late_count, "early_leave_count": r.early_leave_count,
                       "night_shift_count": r.night_shift_count,
                       "total_work_minutes": r.total_work_minutes} for r in rows]}


class CertificateBody(BaseModel):
    cert_type: str  # EMPLOYMENT | CAREER
    purpose: str | None = None


def _cert_pdf(emp: Employee, issue, *, dept_name: str | None = None,
              org_name: str | None = None, logo: str | None = None) -> bytes:
    """증명서 PDF — 발급기관은 기관 설정(DB > env ORG_NAME, 미설정 시 폴백 "본 의료기관장").

    표기 결함 방지: "OO병원장" 견본 문구 하드코딩 금지·부서는 ID 숫자가 아니라 부서명.
    """
    from datetime import date

    from erp.common.pdf import render_pdf
    is_employment = issue.cert_type == "EMPLOYMENT"
    org = (org_name or "").strip()
    return render_pdf("certificate.html", {
        "issue_no": f"CERT-{issue.id:06d}",
        "title": "재 직 증 명 서" if is_employment else "경 력 증 명 서",
        "kind_text": "재직" if is_employment else "근무한",
        "name": emp.name, "employee_no": emp.employee_no,
        "department": dept_name or "-", "position": emp.position or "-",
        "hired_on": emp.hired_on,
        "until": "현재" if emp.resigned_on is None else emp.resigned_on,
        "purpose": issue.purpose, "issued_on": date.today().isoformat(),
        "hospital_name": f"{org}장" if org else "본 의료기관장", "logo": logo,
    })


async def _dept_name(session: AsyncSession, department_id: int | None) -> str | None:
    """부서명 조회 — 증명서 표기용(common.departments)."""
    if department_id is None:
        return None
    from erp.common.models import Department
    return await session.scalar(
        select(Department.name).where(Department.id == department_id))


@router.post("/certificates", status_code=201)
async def issue_certificate(body: CertificateBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """증명서 발급 기록 (FR-23) — PDF는 /certificates/{id}/pdf 로 다운로드/재발급."""
    emp_id = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
    if emp_id is None:
        raise ForbiddenError()
    issue = await _cert.issue(session, employee_id=emp_id, cert_type=body.cert_type,
                              issued_to=user.user_id, purpose=body.purpose)
    await session.flush()
    return {"id": issue.id, "cert_type": issue.cert_type, "purpose": issue.purpose}


@router.get("/certificates")
async def list_certificates(user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """본인 증명서 발급 이력 (FR-23)."""
    from erp.hr.models import CertificateIssue
    emp_id = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
    if emp_id is None:
        raise ForbiddenError()
    rows = (await session.execute(
        select(CertificateIssue).where(CertificateIssue.employee_id == emp_id)
        .order_by(CertificateIssue.id.desc()))).scalars().all()
    return {"items": [{"id": c.id, "cert_type": c.cert_type, "purpose": c.purpose,
                       "issued_at": c.issued_at} for c in rows]}


@router.get("/certificates/{issue_id}/pdf")
async def certificate_pdf(issue_id: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """증명서 PDF 다운로드·재발급 (FR-23) — 본인 발급분만, PRINT 접속기록."""
    from fastapi.responses import Response

    from erp.hr.models import CertificateIssue
    emp_id = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
    issue = await session.get(CertificateIssue, issue_id)
    if issue is None or emp_id is None or issue.employee_id != emp_id:
        raise ForbiddenError("본인 증명서만 발급할 수 있습니다")
    emp = (await session.execute(
        select(Employee).where(Employee.id == emp_id))).scalar_one()
    await _audit.record_access(session, actor=user.user_id, action="PRINT",
                               entity="hr.certificate_issues", target_id=str(issue.id),
                               route=f"/hr/certificates/{issue_id}/pdf")
    from erp.common.org_service import org_display_name, org_logo_data_uri
    return Response(content=_cert_pdf(emp, issue,
                                      dept_name=await _dept_name(session, emp.department_id),
                                      org_name=await org_display_name(session),
                                      logo=await org_logo_data_uri(session)),
                    media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="certificate-{issue.id}.pdf"'})


@router.get("/payrolls/me/{yyyymm}/pdf")
async def my_payslip_pdf(yyyymm: YearMonth,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """급여명세 PDF (FR-07b) — 확정본만, PRINT 접속기록."""
    from fastapi.responses import Response

    from erp.common.pdf import render_pdf
    from erp.errors import ValidationError
    emp_id = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
    if emp_id is None:
        raise ForbiddenError()
    record = (await session.execute(
        select(PayrollRecord).where(PayrollRecord.employee_id == emp_id,
                                    PayrollRecord.yyyymm == yyyymm,
                                    PayrollRecord.confirmed_at.isnot(None))
    )).scalar_one_or_none()
    if record is None:
        raise ValidationError("확정된 명세가 없습니다")
    emp = (await session.execute(
        select(Employee).where(Employee.id == emp_id))).scalar_one()
    lines = (await session.execute(
        select(PayrollLine).where(PayrollLine.record_id == record.id))).scalars().all()
    cat = await _item_catalog(session)
    earnings = _payslip_rows(lines, cat, "EARNING")
    deductions = _payslip_rows(lines, cat, "DEDUCTION")
    earning = sum(r["amount"] for r in earnings)
    deduction = sum(r["amount"] for r in deductions)
    # 의료진 성과급 실적 배분 근거(P2) — INCENTIVE 라인 있을 때만·본인 명세 경로(M7)
    incentive_detail = None
    if any(ln.item_code == "INCENTIVE" for ln in lines):
        incentive_detail = await _incentive_detail(session, emp_id, yyyymm)
    await _audit.record_access(session, actor=user.user_id, action="PRINT",
                               entity="hr.payroll_records", target_id=str(record.id),
                               route=f"/hr/payrolls/me/{yyyymm}/pdf")
    from erp.common.org_service import org_display_name, org_logo_data_uri
    pdf = render_pdf("payslip.html", {
        "yyyymm": yyyymm, "name": emp.name, "employee_no": emp.employee_no,
        "birth_date": emp.birth_date.strftime("%Y-%m-%d") if emp.birth_date else None,
        "position": emp.position, "incentive_detail": incentive_detail,
        "org_name": await org_display_name(session),   # 발급기관 표기(기관 설정)
        "logo": await org_logo_data_uri(session),      # 기관 로고(첨부)

        "paid_on": record.paid_on.strftime("%Y-%m-%d") if record.paid_on else None,
        "earnings": earnings, "deductions": deductions,
        "total_earning": earning, "total_deduction": deduction,
        "net": earning - deduction,
        "confirmed_at": record.confirmed_at.strftime("%Y-%m-%d"),
    })
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="payslip-{yyyymm}.pdf"'})


# ── 퇴직급여충당부채 (월별 적립) ─────────────────────────────────
@router.post("/retirement/accrue/{yyyymm}")
async def accrue_retirement(yyyymm: YearMonth,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """월별 퇴직급여충당부채 전입 — 전 직원 추계액 증가분을 비용·부채 전표화."""
    _scope(user, "hr.payroll", "write")
    from erp.hr.retirement_service import RetirementService
    out = await RetirementService().accrue_monthly(session, period=yyyymm, user_id=user.user_id)
    await session.commit()
    return out


@router.get("/retirement/provisions/{yyyymm}")
async def retirement_provisions(yyyymm: YearMonth,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """기간 퇴직급여충당 적립 현황 — 직원별 추계액·전입·누적."""
    _require_all(_scope(user, "hr.payroll", "read"))   # 2026-10-09 전체 흐름 검토 G — 전 직원 추계액은 ALL만
    from erp.hr.retirement_service import RetirementService
    return await RetirementService().provisions(session, period=yyyymm)


@router.get("/retirement/dbo/preview/{yyyymm}")
async def dbo_preview(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """확정급여채무(DBO) 보험수리 평가 미리보기(K-IFRS 1019) — 명목 대비 재측정(OCI)."""
    _require_all(_scope(user, "hr.payroll", "read"))   # 2026-10-09 전체 흐름 검토 G — 전사 평가는 ALL만
    from erp.hr.retirement_service import RetirementService
    return await RetirementService().dbo_preview(session, period=yyyymm)


@router.post("/retirement/dbo/post/{yyyymm}")
async def dbo_post(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    """DBO 보험수리적 재측정 전기(멱등) — 차액을 OCI(3410)/충당부채(2120)."""
    _scope(user, "hr.payroll", "write")
    from erp.hr.retirement_service import RetirementService
    out = await RetirementService().dbo_post(session, period=yyyymm, user_id=user.user_id)
    await session.commit()
    return out


# ── 원내 교육이수 (Tier3·0228) — 과정·회차·이수·현황 ─────────────────────────
def _training():
    from erp.hr.training_service import TrainingService
    return TrainingService()


class CourseBody(BaseModel):
    code: str
    title: str
    category: str = "JOB"
    cycle_months: int = 0
    target: str = "ALL"
    target_value: str | None = None
    mandatory: bool = True
    note: str | None = None


@router.post("/training/courses", status_code=201)
async def create_course(body: CourseBody, user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """교육 과정 마스터 등록 — HR 관리자."""
    _scope(user, "hr.employees", "write")
    c = await _training().create_course(session, actor=user.user_id, **body.model_dump())
    await session.commit()
    return {"id": c.id, "code": c.code}


@router.get("/training/courses")
async def list_courses(active: bool | None = None,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.employees", "read")
    return {"items": await _training().list_courses(session, active=active)}


class CourseActiveBody(BaseModel):
    active: bool


@router.patch("/training/courses/{course_id}/active")
async def set_course_active(course_id: int, body: CourseActiveBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.employees", "write")
    c = await _training().set_course_active(session, actor=user.user_id, course_id=course_id,
                                            active=body.active)
    await session.commit()
    return {"id": c.id, "active": c.active}


class SessionBody(BaseModel):
    held_on: date
    hours: float = 1
    method: str = "OFFLINE"
    instructor: str | None = None
    location: str | None = None
    note: str | None = None


@router.post("/training/courses/{course_id}/sessions", status_code=201)
async def create_session(course_id: int, body: SessionBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.employees", "write")
    s = await _training().create_session(session, actor=user.user_id, course_id=course_id,
                                         **body.model_dump())
    await session.commit()
    return {"id": s.id, "held_on": s.held_on}


@router.get("/training/courses/{course_id}/sessions")
async def list_sessions(course_id: int, user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.employees", "read")
    return {"items": await _training().list_sessions(session, course_id=course_id)}


class CompletionBody(BaseModel):
    employee_id: int
    completed_on: date | None = None
    score: float | None = None
    cert_no: str | None = None
    note: str | None = None


@router.post("/training/sessions/{session_id}/completions", status_code=201)
async def record_completion(session_id: int, body: CompletionBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.employees", "write")
    rec = await _training().record_completion(session, actor=user.user_id, session_id=session_id,
                                              **body.model_dump())
    await session.commit()
    return {"id": rec.id, "valid_until": rec.valid_until}


class BulkCompletionBody(BaseModel):
    employee_ids: list[int]


@router.post("/training/sessions/{session_id}/completions/bulk")
async def record_bulk(session_id: int, body: BulkCompletionBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """회차 참석자 명단 일괄 이수 등록(멱등)."""
    _scope(user, "hr.employees", "write")
    out = await _training().record_bulk(session, actor=user.user_id, session_id=session_id,
                                        employee_ids=body.employee_ids)
    await session.commit()
    return out


@router.get("/training/completions")
async def list_completions(employee_id: int,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    scope = _scope(user, "hr.employees", "read")
    await _assert_employee_access(session, user, scope, employee_id)   # 2026-10-09 전체 흐름 검토 G — SELF/DEPT 행 scope
    return {"items": await _training().completions(session, employee_id=employee_id)}


@router.get("/training/status")
async def training_status(user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """필수 과정 × 대상 재직자 이수 현황 매트릭스(OK/임박/만료/미이수) — 인증평가 대비."""
    _scope(user, "hr.compensation", "read")
    return await _training().status(session)


# ── 인사평가 (Tier3·0229) — 차수·항목·배정·자기평가·채점·확정 ────────────────
def _eval_svc():
    from erp.hr.evaluation_service import EvaluationService
    return EvaluationService()


class CycleBody(BaseModel):
    period: str
    title: str
    cycle_type: str = "ANNUAL"
    start_on: date | None = None
    end_on: date | None = None
    grade_bands: list[dict] | None = None


@router.post("/eval/cycles", status_code=201)
async def create_eval_cycle(body: CycleBody, user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.evaluation", "write")
    c = await _eval_svc().create_cycle(session, actor=user.user_id, **body.model_dump())
    await session.commit()
    return {"id": c.id, "period": c.period}


@router.get("/eval/cycles")
async def list_eval_cycles(user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.evaluation", "read")
    return {"items": await _eval_svc().list_cycles(session)}


class CriterionBody(BaseModel):
    title: str
    kind: str = "COMPETENCY"
    weight: float = 1
    max_score: float = 100
    sort_order: int = 0


@router.post("/eval/cycles/{cycle_id}/criteria", status_code=201)
async def add_criterion(cycle_id: int, body: CriterionBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.evaluation", "write")
    cr = await _eval_svc().add_criterion(session, actor=user.user_id, cycle_id=cycle_id,
                                         **body.model_dump())
    await session.commit()
    return {"id": cr.id}


@router.get("/eval/cycles/{cycle_id}/criteria")
async def list_criteria(cycle_id: int, user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.evaluation", "read")
    crits = await _eval_svc().criteria(session, cycle_id)
    return {"items": [{"id": c.id, "kind": c.kind, "title": c.title, "weight": float(c.weight),
                       "max_score": float(c.max_score), "sort_order": c.sort_order}
                      for c in crits]}


@router.post("/eval/cycles/{cycle_id}/open")
async def open_eval_cycle(cycle_id: int, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.evaluation", "write")
    c = await _eval_svc().open_cycle(session, actor=user.user_id, cycle_id=cycle_id)
    await session.commit()
    return {"id": c.id, "status": c.status}


@router.post("/eval/cycles/{cycle_id}/close")
async def close_eval_cycle(cycle_id: int, user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.evaluation", "write")
    c = await _eval_svc().close_cycle(session, actor=user.user_id, cycle_id=cycle_id)
    await session.commit()
    return {"id": c.id, "status": c.status}


class AssignBulkBody(BaseModel):
    assignments: list[dict]   # [{employee_id, evaluator_id?}]


@router.post("/eval/cycles/{cycle_id}/assign")
async def assign_evaluations(cycle_id: int, body: AssignBulkBody,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.evaluation", "write")
    out = await _eval_svc().assign_bulk(session, actor=user.user_id, cycle_id=cycle_id,
                                        assignments=body.assignments)
    await session.commit()
    return out


@router.get("/eval/cycles/{cycle_id}/evaluations")
async def list_evaluations(cycle_id: int, user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """평가 목록 — ALL 전체·DEPT 본부서·SELF 본인."""
    scope = _scope(user, "hr.evaluation", "read")
    dept = emp = None
    if scope.kind == ScopeKind.DEPT:
        dept = user.dept_id
    elif scope.kind == ScopeKind.SELF:
        emp = (await session.execute(_self_employee_q(user))).scalar_one_or_none() or -1
    return {"items": await _eval_svc().list_evaluations(session, cycle_id=cycle_id,
                                                        dept_id=dept, employee_id=emp)}


@router.get("/eval/evaluations/{eval_id}")
async def eval_detail(eval_id: int, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    scope = _scope(user, "hr.evaluation", "read")
    ev = await _eval_svc()._get_eval(session, eval_id)
    await _assert_employee_access(session, user, scope, ev.employee_id)
    return await _eval_svc().get_detail(session, eval_id)


class ScoresBody(BaseModel):
    scores: list[dict]        # [{criterion_id, score, note?}]
    comment: str | None = None


@router.post("/eval/evaluations/{eval_id}/self")
async def submit_self(eval_id: int, body: ScoresBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """자기평가 제출 — 본인만(read 게이트 후 서비스에서 대상 본인 검증)."""
    _scope(user, "hr.evaluation", "read")
    self_emp = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
    ev = await _eval_svc().submit_self(session, eval_id=eval_id, self_emp=self_emp,
                                       scores=body.scores, comment=body.comment)
    await session.commit()
    return {"id": ev.id, "status": ev.status, "self_total": float(ev.self_total or 0)}


@router.post("/eval/evaluations/{eval_id}/score")
async def submit_eval(eval_id: int, body: ScoresBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """평가자 채점 — 지정 평가자 또는 부서/전체 권한자(서비스 행 검증)."""
    scope = _scope(user, "hr.evaluation", "read")   # 채점 권한은 서비스에서 평가자/부서 검증
    ev = await _eval_svc().submit_eval(session, eval_id=eval_id, actor=user.user_id,
                                       actor_scope=scope, scores=body.scores, comment=body.comment)
    await session.commit()
    return {"id": ev.id, "status": ev.status, "eval_total": float(ev.eval_total or 0)}


class ConfirmBody(BaseModel):
    final_grade: str | None = None


@router.post("/eval/evaluations/{eval_id}/confirm")
async def confirm_eval(eval_id: int, body: ConfirmBody | None = None,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    scope = _scope(user, "hr.evaluation", "write")
    ev = await _eval_svc()._get_eval(session, eval_id)
    await _assert_employee_access(session, user, scope, ev.employee_id)
    ev = await _eval_svc().confirm(session, eval_id=eval_id, actor=user.user_id,
                                   final_grade=body.final_grade if body else None)
    await session.commit()
    return {"id": ev.id, "status": ev.status, "final_grade": ev.final_grade}


# ── 복리후생 (Tier3·0230) — 제도·부여·신청/승인/지급·잔액 ────────────────────
def _welfare():
    from erp.hr.welfare_service import WelfareService
    return WelfareService()


class ProgramBody(BaseModel):
    code: str
    title: str
    benefit_type: str = "POINT"
    unit: str = "KRW"
    annual_limit: float = 0
    target: str = "ALL"
    target_value: str | None = None
    requires_receipt: bool = True
    note: str | None = None


@router.post("/welfare/programs", status_code=201)
async def create_program(body: ProgramBody, user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.welfare", "admin")
    p = await _welfare().create_program(session, actor=user.user_id, **body.model_dump())
    await session.commit()
    return {"id": p.id, "code": p.code}


@router.get("/welfare/programs")
async def list_programs(active: bool | None = None,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.welfare", "read")
    return {"items": await _welfare().list_programs(session, active=active)}


class ProgramActiveBody(BaseModel):
    active: bool


@router.patch("/welfare/programs/{program_id}/active")
async def set_program_active(program_id: int, body: ProgramActiveBody,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.welfare", "admin")
    p = await _welfare().set_program_active(session, actor=user.user_id, program_id=program_id,
                                            active=body.active)
    await session.commit()
    return {"id": p.id, "active": p.active}


class GrantBulkBody(BaseModel):
    program_id: int
    year: int
    granted: float
    employee_ids: list[int]


@router.post("/welfare/grants")
async def grant_bulk(body: GrantBulkBody, user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.welfare", "admin")
    out = await _welfare().grant_bulk(session, actor=user.user_id, program_id=body.program_id,
                                      year=body.year, granted=body.granted,
                                      employee_ids=body.employee_ids)
    await session.commit()
    return out


@router.get("/welfare/balance")
async def welfare_balance(year: int, employee_id: int | None = None,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """복리후생 잔액 — 본인(SELF) 또는 타인(ALL/DEPT 권한자·행 검증)."""
    scope = _scope(user, "hr.welfare", "read")
    if employee_id is not None:
        # 2026-10-09 시나리오 S-F: 종전엔 eid=employee_id로 시작해 DEPT 권한자가 타 부서 직원 id를
        # 주면 `employee_id != eid`가 거짓 → 행 검증을 건너뛰었다. 지정했으면 항상 검증한다.
        await _assert_employee_access(session, user, scope, employee_id)
        eid = employee_id
    else:
        eid = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
        if eid is None:
            raise ValidationError("직원 정보가 연결되지 않은 계정입니다")
    return {"year": year, "employee_id": eid,
            "items": await _welfare().balance(session, employee_id=eid, year=year)}


class ClaimBody(BaseModel):
    program_id: int
    amount: float
    claim_on: date
    reason: str | None = None


@router.post("/welfare/claims", status_code=201)
async def create_claim(body: ClaimBody, user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """복리후생 신청 — 본인 명의(SELF 강제)."""
    _scope(user, "hr.welfare", "write")
    self_emp = (await session.execute(_self_employee_q(user))).scalar_one_or_none()
    if self_emp is None:
        raise ValidationError("직원 정보가 연결되지 않은 계정입니다")
    c = await _welfare().claim(session, actor=user.user_id, program_id=body.program_id,
                              employee_id=self_emp, amount=body.amount, claim_on=body.claim_on,
                              reason=body.reason)
    await session.commit()
    return {"id": c.id, "status": c.status}


@router.get("/welfare/claims")
async def list_claims(status: str | None = None, year: int | None = None,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """신청 목록 — ALL 전체·DEPT 본부서·SELF 본인."""
    scope = _scope(user, "hr.welfare", "read")
    emp = None
    if scope.kind == ScopeKind.SELF:
        emp = (await session.execute(_self_employee_q(user))).scalar_one_or_none() or -1
    elif scope.kind == ScopeKind.DEPT:
        from erp.hr.models import Employee as _E
        ids = (await session.scalars(select(_E.id).where(
            _E.department_id == user.dept_id))).all()
        items = await _welfare().list_claims(session, status=status, year=year)
        return {"items": [i for i in items if i["employee_id"] in set(ids)]}
    return {"items": await _welfare().list_claims(session, status=status, employee_id=emp,
                                                  year=year)}


class ClaimDecideBody(BaseModel):
    approve: bool


@router.post("/welfare/claims/{claim_id}/decide")
async def decide_claim(claim_id: int, body: ClaimDecideBody,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    _scope(user, "hr.welfare", "admin")
    c = await _welfare().decide(session, actor=user.user_id, claim_id=claim_id, approve=body.approve)
    await session.commit()
    return {"id": c.id, "status": c.status}


class ClaimPayBody(BaseModel):
    paid_on: date | None = None


@router.post("/welfare/claims/{claim_id}/pay")
async def pay_claim(claim_id: int, body: ClaimPayBody | None = None,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """지급 — DR 5330 복리후생비 / CR 1110 현금(멱등)."""
    _scope(user, "hr.welfare", "admin")
    c = await _welfare().pay(session, actor=user.user_id, claim_id=claim_id,
                            paid_on=body.paid_on if body else None)
    await session.commit()
    return {"id": c.id, "status": c.status, "voucher_ref": c.voucher_ref}


# ── 채용 ATS (공고·지원자·전형·P3-9·Tier3) ──────────────────────
def _recruit():
    from erp.hr.recruit_service import RecruitService
    return RecruitService()


class PostingBody(BaseModel):
    title: str
    posted_on: date
    department_id: int | None = None
    employment_type: str = "REGULAR"
    headcount: int = 1
    close_on: date | None = None
    note: str | None = None


@router.post("/job-postings", status_code=201)
async def create_posting(body: PostingBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """채용 공고 등록 — 인사 관리(hr.recruit)."""
    _scope(user, "hr.recruit", "write")
    p = await _recruit().create_posting(
        session, title=body.title, posted_on=body.posted_on, department_id=body.department_id,
        employment_type=body.employment_type, headcount=body.headcount, close_on=body.close_on,
        note=body.note, actor=user.user_id)
    return {"id": p.id, "status": p.status}


@router.get("/job-postings")
async def list_postings(status: str | None = None,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """채용 공고 목록(지원자 수 포함)."""
    _scope(user, "hr.recruit", "read")
    return {"items": await _recruit().list_postings(session, status=status)}


class PostingStatusBody(BaseModel):
    status: str            # OPEN|CLOSED|FILLED


@router.post("/job-postings/{posting_id}/status")
async def set_posting_status(posting_id: int, body: PostingStatusBody,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """공고 상태 변경(마감·충원완료)."""
    _scope(user, "hr.recruit", "write")
    p = await _recruit().set_posting_status(session, posting_id=posting_id, status=body.status)
    return {"id": p.id, "status": p.status}


@router.get("/job-postings/{posting_id}/funnel")
async def posting_funnel(posting_id: int,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """공고 전형 퍼널 — 단계별 인원."""
    _scope(user, "hr.recruit", "read")
    return await _recruit().funnel(session, posting_id=posting_id)


class ApplicantBody(BaseModel):
    posting_id: int
    name: str
    applied_on: date
    contact: str | None = None
    source: str | None = None
    note: str | None = None


@router.post("/applicants", status_code=201)
async def add_applicant(body: ApplicantBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """지원자 접수 — 개인정보 최소수집(hr.recruit)."""
    _scope(user, "hr.recruit", "write")
    a = await _recruit().add_applicant(
        session, posting_id=body.posting_id, name=body.name, applied_on=body.applied_on,
        contact=body.contact, source=body.source, note=body.note, actor=user.user_id)
    return {"id": a.id, "stage": a.stage}


@router.get("/applicants")
async def list_applicants(posting_id: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """공고별 지원자 목록."""
    _scope(user, "hr.recruit", "read")
    return {"items": await _recruit().list_applicants(session, posting_id=posting_id)}


class StageBody(BaseModel):
    stage: str             # APPLIED|SCREENING|INTERVIEW|OFFER|HIRED|REJECTED


@router.post("/applicants/{applicant_id}/stage")
async def advance_applicant(applicant_id: int, body: StageBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """전형 단계 진행/탈락."""
    _scope(user, "hr.recruit", "write")
    a = await _recruit().advance(session, applicant_id=applicant_id, stage=body.stage)
    return {"id": a.id, "stage": a.stage}
