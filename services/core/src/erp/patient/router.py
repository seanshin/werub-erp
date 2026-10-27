"""Patient 라우터 — design §4.3."""
from datetime import date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.policy import ScopeKind, require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.errors import ForbiddenError, ValidationError
from erp.patient.models import Reservation
from erp.patient.receivable_service import PatientReceivableService
from erp.patient.service import (
    ClosingService, MedicalExpenseReportService, PatientService, PaymentService,
    ReceptionService, RefundService,
)

router = APIRouter(prefix="/patients", tags=["patient"])
_patients, _receptions = PatientService(), ReceptionService()
_payments, _closings = PaymentService(), ClosingService()
_patient_ar = PatientReceivableService()
_refunds = RefundService()
_med_expense = MedicalExpenseReportService()
_audit = AuditService()


def _require(user: CurrentUser, action: str):
    """분석 H1 — 기본값 제거, read/write 명시 강제."""
    return require_scope(user, "patient.patients", action)


class PatientCreate(BaseModel):
    chart_no: str
    name: str
    birth_date: date
    phone: str | None = None   # FR-33 — 4필드 외 입력 경로 없음


@router.post("", status_code=201)
async def create_patient(body: PatientCreate,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    p = await _patients.create(session, **body.model_dump())
    return {"id": p.id, "chart_no": p.chart_no}


class PatientSearchBody(BaseModel):
    name: str | None = None
    birth_date: date | None = None
    chart_no: str | None = None
    phone: str | None = None


@router.post("/search")
async def search_patients_post(body: PatientSearchBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """환자 검색(POST) — Phase0 W1: 식별자(성명·차트·전화)가 쿼리스트링으로 uvicorn/nginx
    access log에 남던 GET을 대체(FE 전환·GET은 호환 유지)."""
    return await _search_patients(session, user, name=body.name, birth_date=body.birth_date,
                                  chart_no=body.chart_no, phone=body.phone)


@router.get("")
async def search_patients(name: str | None = None, birth_date: date | None = None,
                          chart_no: str | None = None, phone: str | None = None,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """(호환) GET 검색 — 신규 소비는 POST /patients/search 사용(W1·쿼리스트링 로그)."""
    return await _search_patients(session, user, name=name, birth_date=birth_date,
                                  chart_no=chart_no, phone=phone)


async def _search_patients(session: AsyncSession, user: CurrentUser, *, name=None,
                           birth_date=None, chart_no=None, phone=None):
    scope = _require(user, "read")
    dept_codes = None
    if scope.kind == ScopeKind.DEPT:   # 부서 경계(§6.2) — 본부서 접수 환자만(전 환자 명단 enumeration 차단)
        from erp.common.dept_scope import dept_code_candidates
        dept_codes = await dept_code_candidates(session, scope.dept_id)
    rows = await _patients.search(session, name=name, birth_date=birth_date,
                                  chart_no=chart_no, phone=phone, dept_codes=dept_codes)
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="patient.patients", route="/patients")
    from erp.common.crypto import column_aad, decrypt_column, mask_phone
    items = []
    for p in rows:
        phone_masked = None
        if p.phone_enc is not None:
            phone_masked = mask_phone(decrypt_column(
                p.phone_enc, aad=column_aad("patient.patients.phone", p.id)))
        items.append({"id": p.id, "chart_no": p.chart_no, "name": p.name,
                      "birth_date": p.birth_date, "phone": phone_masked})
    return {"items": items}


class ReceptionBody(BaseModel):
    patient_id: int
    dept_code: str
    doctor_id: int | None = None
    received_on: date


@router.post("/receptions", status_code=201)
async def receive(body: ReceptionBody,
                  user: CurrentUser = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    rec = await _receptions.receive(session, **body.model_dump())
    return {"id": rec.id}


@router.get("/receptions")
async def list_receptions(on: date,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """일자별 접수 목록 — 수납 화면용 (동일 모듈 내 조인은 경계 원칙 허용)."""
    scope = _require(user, "read")
    from sqlalchemy import select

    from erp.common.policy import ScopeKind
    from erp.patient.models import Patient, Payment, Reception
    q = (select(Reception, Patient.name, Patient.chart_no)
         .join(Patient, Patient.id == Reception.patient_id)
         .where(Reception.received_on == on))
    if scope.kind == ScopeKind.DEPT:        # 부서 경계 — MANAGER는 본부서 접수만(IDOR)
        from erp.common.dept_scope import dept_code_candidates   # dept_id↔dept_code 매핑(H1)
        q = q.where(Reception.dept_code.in_(await dept_code_candidates(session, scope.dept_id)))
    rows = (await session.execute(q.order_by(Reception.id.desc()))).all()
    await _audit.record_access(session, actor=user.user_id, action="VIEW",   # PII(성명·차트) 열람추적
                               entity="patient.patients", route="/patients/receptions")
    paid_ids = set((await session.execute(
        select(Payment.reception_id).where(Payment.paid_on == on,
                                           Payment.status == "PAID"))).scalars().all())
    return {"items": [
        {"id": r.Reception.id, "chart_no": r.chart_no, "name": r.name,
         "dept_code": r.Reception.dept_code, "paid": r.Reception.id in paid_ids}
        for r in rows]}


class PaymentBody(BaseModel):
    reception_id: int
    amount: float
    method: str = "CARD"
    paid_on: date


@router.post("/payments", status_code=201)
async def pay(body: PaymentBody,
              user: CurrentUser = Depends(get_current_user),
              session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    p = await _payments.pay(session, **body.model_dump(), created_by=user.user_id)
    return {"id": p.id}


@router.get("/payments")
async def list_payments(on: date,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """일자별 정상(PAID) 수납 목록 — 마감 정정(FR-54) 취소 대상 선택용. 마감 통제 정책 게이트."""
    _require_closing(user, "read")
    from sqlalchemy import select

    from erp.patient.models import Patient, Payment, Reception
    rows = (await session.execute(
        select(Payment.id, Payment.amount, Payment.method, Payment.reception_id,
               Patient.chart_no, Patient.name)
        .join(Reception, Reception.id == Payment.reception_id)
        .join(Patient, Patient.id == Reception.patient_id)
        .where(Payment.paid_on == on, Payment.status == "PAID")
        .order_by(Payment.id))).all()
    await _audit.record_access(session, actor=user.user_id, action="VIEW",   # PII 열람추적
                               entity="patient.patients", route="/patients/payments")
    return {"items": [{"id": r.id, "amount": float(r.amount), "method": r.method,
                       "reception_id": r.reception_id, "chart_no": r.chart_no, "name": r.name}
                      for r in rows]}


class ReservationBody(BaseModel):
    patient_id: int
    dept_code: str
    doctor_id: int
    reserved_at: datetime


@router.get("/reservations")
async def list_reservations(on: date,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """일자별 예약 (FR-17 캘린더)."""
    scope = _require(user, "read")
    from sqlalchemy import cast, Date as SaDate, select

    from erp.common.policy import ScopeKind
    from erp.patient.models import Patient
    q = (select(Reservation, Patient.name, Patient.chart_no)
         .join(Patient, Patient.id == Reservation.patient_id)
         .where(cast(Reservation.reserved_at, SaDate) == on))
    if scope.kind == ScopeKind.DEPT:        # 부서 경계 — MANAGER는 본부서 예약만(IDOR)
        from erp.common.dept_scope import dept_code_candidates   # dept_id↔dept_code 매핑(H1)
        q = q.where(Reservation.dept_code.in_(await dept_code_candidates(session, scope.dept_id)))
    rows = (await session.execute(q.order_by(Reservation.reserved_at))).all()
    await _audit.record_access(session, actor=user.user_id, action="VIEW",   # PII(성명·차트) 열람추적
                               entity="patient.patients", route="/patients/reservations")
    return {"items": [
        {"id": r.Reservation.id, "chart_no": r.chart_no, "name": r.name,
         "dept_code": r.Reservation.dept_code, "doctor_id": r.Reservation.doctor_id,
         "reserved_at": r.Reservation.reserved_at, "status": r.Reservation.status}
        for r in rows]}


@router.post("/reservations", status_code=201)
async def reserve(body: ReservationBody,
                  user: CurrentUser = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    _require(user, "write")
    r = Reservation(**body.model_dump())
    session.add(r)
    await session.flush()
    return {"id": r.id, "status": r.status}


# 예약 상태 전이 — BOOKED에서만 방문/노쇼/취소로. 종결 상태는 재전이 불가(원무 운영 통제).
_RESV_TRANSITIONS = {"BOOKED": {"VISITED", "NOSHOW", "CANCELED"}}


class ReservationStatusBody(BaseModel):
    status: str          # VISITED | NOSHOW | CANCELED


@router.patch("/reservations/{reservation_id}/status")
async def update_reservation_status(reservation_id: int, body: ReservationStatusBody,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """예약 상태 전이 (FR-17) — 방문(VISITED)·노쇼(NOSHOW)·취소(CANCELED). 노쇼 관리·통계 기반."""
    scope = _require(user, "write")
    r = await session.get(Reservation, reservation_id)
    if r is None:
        raise ValidationError("존재하지 않는 예약입니다")
    if scope.kind == ScopeKind.DEPT:        # 부서 경계(IDOR) — 본부서 예약만 처리
        from erp.common.dept_scope import dept_code_candidates
        if r.dept_code not in await dept_code_candidates(session, scope.dept_id):
            raise ForbiddenError("본인 부서의 예약만 처리할 수 있습니다")
    nxt = (body.status or "").upper()
    allowed = _RESV_TRANSITIONS.get(r.status, set())
    if nxt not in allowed:
        raise ValidationError("허용되지 않는 예약 상태 전이입니다",
                              {"from": r.status, "to": nxt, "allowed": sorted(allowed)})
    r.status = nxt
    await _audit.record_audit(session, actor=user.user_id, action="UPDATE",
                              entity="patient.reservations", target_id=str(reservation_id))
    await session.commit()
    return {"id": r.id, "status": r.status}


def _require_closing(user: CurrentUser, action: str):
    """일마감 권한 — 수납 입력(patient.patients)과 분리한 회계 통제 게이트."""
    return require_scope(user, "patient.closing", action)


@router.get("/closings")
async def list_closings(yyyymm: str | None = None, limit: int = 100,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """마감 이력 조회 (FR-18) — 최신순, yyyymm='YYYY-MM' 월 필터 옵션."""
    _require_closing(user, "read")
    return {"items": await _closings.history(session, yyyymm=yyyymm, limit=min(limit, 500))}


@router.get("/closings/preview")
async def preview_closing(on: date,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """마감 전 일계표 미리보기 (FR-18) — 그날 수납 합계·건수·수단별·마감 여부·직무분리 경고."""
    _require_closing(user, "read")
    return await _closings.preview(session, on, viewer=user.user_id)


@router.get("/closings/by-teller")
async def closing_by_teller(on: date,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """수납원별 일일 마감(시재 대사) — 입력자별 수납·환불·현금시재. 수납원 시재 책임 가시화."""
    _require_closing(user, "read")
    return await _closings.tellers_summary(session, on)


@router.post("/closings/{closing_on}")
async def close_day(closing_on: date, force: bool = False,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """일마감 — force=true 시 직무분리(본인 단독 입력) 경고를 무시하고 강제 마감(감사 기록)."""
    _require_closing(user, "write")
    c = await _closings.close_day(session, closing_on=closing_on, actor=user.user_id,
                                  allow_self_close=force)
    return {"id": c.id, "seq": c.seq, "total": float(c.total_amount),
            "voucher_ref": c.voucher_ref}


class AmendBody(BaseModel):
    reason: str
    correction_payments: list[dict] = []


@router.post("/closings/{closing_on}/amend")
async def amend_closing(closing_on: date, body: AmendBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """FR-54 — 마감 정정 권한(patient.closing:approve, ADMIN) 단일 정책 게이트."""
    _require_closing(user, "approve")
    c = await _closings.amend(session, closing_on=closing_on, actor=user.user_id,
                              correction_payments=body.correction_payments,
                              reason=body.reason)
    return {"id": c.id, "seq": c.seq, "total": float(c.total_amount),
            "voucher_ref": c.voucher_ref}


# ── 환자 본인부담 발생주의 미수금 (원무 심화) ──────────────────────
@router.post("/accrual/run")
async def run_patient_accrual(care_month: str,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """환자 본인부담 발생 — 해당 진료월 전 chart 미수금 계상(delta 멱등). 회계통제(patient.closing write).

    config patient_accrual_enabled OFF면 no-op(enabled=false). 현행 현금주의 유지."""
    _require_closing(user, "write")
    out = await _patient_ar.accrue_all(session, care_month=care_month, actor=user.user_id)
    if out.get("posted"):
        await _audit.record_audit(session, actor=user.user_id, action="PATIENT_ACCRUAL",
                                  entity="finance.receivables", target_id=care_month, after=out)
    return out


@router.post("/accrual/open-cutover")
async def open_accrual_cutover(cutover_month: str,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """발생주의 전환 1회 개통 — cutover 이전 미수납분을 DR1131/CR4110 개통전기(멱등). 회계통제.

    현금주의 하 이미 수납분은 수익 인식돼 불건드림. 활성화 시 1회 실행(회계사 사인오프 후)."""
    _require_closing(user, "write")
    out = await _patient_ar.open_cutover(session, cutover_month=cutover_month, actor=user.user_id)
    if out.get("opened"):
        await _audit.record_audit(session, actor=user.user_id, action="PATIENT_ACCRUAL_CUTOVER",
                                  entity="finance.receivables", target_id=cutover_month, after=out)
    return out


@router.get("/accrual/aging")
async def patient_ar_aging(as_of: date | None = None,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """환자 본인부담 미수금 연령분석 — source_module='patient' 채권만."""
    _require_closing(user, "read")
    from erp.finance.settlement_service import SettlementService
    return await SettlementService().aging(session, as_of=as_of or date.today(),
                                           kind="AR", source_module="patient")


class RefundBody(BaseModel):
    chart_no: str
    amount: float
    reason: str
    refunded_on: date
    method: str = "CASH"
    reception_id: int | None = None


@router.post("/refunds", status_code=201)
async def create_refund(body: RefundBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """환불(과오납 반환) — 즉시 역전표(현금 반환). 회계통제(patient.closing write)."""
    _require_closing(user, "write")
    ref = await _refunds.refund(session, chart_no=body.chart_no, amount=body.amount,
                                reason=body.reason, refunded_on=body.refunded_on,
                                method=body.method, reception_id=body.reception_id,
                                actor=user.user_id)
    await _audit.record_audit(session, actor=user.user_id, action="REFUND",
                              entity="patient.refunds", target_id=str(ref.id),
                              after={"chart_no": body.chart_no, "amount": float(body.amount)})
    return {"id": ref.id, "voucher_ref": ref.voucher_ref}


# ── 환자 미수금 회수 — 독촉·분납·개별 상각 (마이그 0209·회계통제 patient.closing) ──
def _collections():
    from erp.patient.collection_service import PatientCollectionService
    return PatientCollectionService()


@router.get("/receivables/overdue")
async def overdue_receivables(as_of: date | None = None, min_days: int = 1,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """기한 경과 환자 미수 목록 — 독촉 차수·분납 여부 병기(회수 작업 목록)."""
    _require_closing(user, "read")
    # 차트 단위 채무정보 열람 — 접속기록(INV-AUDIT-01 커버리지 갭 G4)
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="patient.receivables", route="/patients/receivables/overdue")
    return await _collections().overdue_list(session, as_of=as_of or date.today(),
                                             min_days=min_days)


class DunningBody(BaseModel):
    method: str = "PHONE"          # SMS | MAIL | PHONE | VISIT
    note: str | None = None
    sent_on: date | None = None


@router.post("/receivables/{receivable_id}/dunning", status_code=201)
async def record_dunning(receivable_id: int, body: DunningBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """독촉(납부안내) 기록 — 차수 자동 증가."""
    _require_closing(user, "write")
    n = await _collections().record_dunning(
        session, receivable_id=receivable_id, method=body.method, note=body.note,
        sent_on=body.sent_on, actor=user.user_id)
    return {"id": n.id, "level": n.level, "outstanding": float(n.outstanding)}


@router.get("/receivables/{receivable_id}/dunning")
async def dunning_history(receivable_id: int,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    _require_closing(user, "read")
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="patient.dunning", target_id=str(receivable_id))
    return {"items": await _collections().dunning_history(session,
                                                          receivable_id=receivable_id)}


class InstallmentBody(BaseModel):
    months: int
    start_month: str               # YYYY-MM
    note: str | None = None


@router.post("/receivables/{receivable_id}/installment-plan", status_code=201)
async def create_installment_plan(receivable_id: int, body: InstallmentBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """분납(납부계획) 합의 — 채권당 ACTIVE 1건."""
    _require_closing(user, "write")
    p = await _collections().create_plan(
        session, receivable_id=receivable_id, months=body.months,
        start_month=body.start_month, note=body.note, actor=user.user_id)
    return {"id": p.id, "monthly_amount": float(p.monthly_amount),
            "total_amount": float(p.total_amount), "status": p.status}


class PlanStatusBody(BaseModel):
    status: str                    # COMPLETED | DEFAULTED | CANCELED


@router.patch("/installment-plans/{plan_id}")
async def update_installment_plan(plan_id: int, body: PlanStatusBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    _require_closing(user, "write")
    p = await _collections().update_plan_status(session, plan_id=plan_id,
                                                status=body.status)
    await _audit.record_audit(session, actor=user.user_id, action="UPDATE",
                              entity="patient.installment_plans", target_id=str(plan_id),
                              after={"status": body.status})
    return {"id": p.id, "status": p.status}


class WriteOffBody(BaseModel):
    reason: str
    on: date | None = None


@router.post("/receivables/{receivable_id}/write-off")
async def write_off_receivable(receivable_id: int, body: WriteOffBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """개별 대손상각 — 충당금(1190) 우선 상계·부족분 5690 / CR 1131. 멱등."""
    _require_closing(user, "write")
    out = await _collections().write_off(session, receivable_id=receivable_id,
                                         reason=body.reason, on=body.on,
                                         actor=user.user_id)
    await _audit.record_audit(session, actor=user.user_id, action="WRITE_OFF",
                              entity="finance.receivables", target_id=str(receivable_id),
                              after={"reason": body.reason,
                                     "amount": out.get("written_off")})
    return out


# ── 진료의뢰·회송 (진료협력센터·P3-3·마이그 0241) ──────────────────
def _referral():
    from erp.patient.referral_service import ReferralService
    return ReferralService()


class ReferralBody(BaseModel):
    referral_type: str                 # REFER_OUT|REFER_IN|BACK_REFER
    partner_institution: str
    referred_on: date
    chart_no: str | None = None
    patient_name: str | None = None
    department: str | None = None
    reason: str | None = None
    has_fee: bool = False
    note: str | None = None


@router.post("/referrals", status_code=201)
async def create_referral(body: ReferralBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """진료의뢰·회송 등록 — 진료협력 담당(patient.referral)."""
    require_scope(user, "patient.referral", "write")
    r = await _referral().create(
        session, referral_type=body.referral_type, partner_institution=body.partner_institution,
        referred_on=body.referred_on, chart_no=body.chart_no, patient_name=body.patient_name,
        department=body.department, reason=body.reason, has_fee=body.has_fee, note=body.note,
        actor=user.user_id)
    return {"id": r.id, "referral_type": r.referral_type}


@router.get("/referrals")
async def list_referrals(referral_type: str | None = None, start: date | None = None,
                         end: date | None = None, status: str | None = None,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """의뢰·회송 목록 — 유형·기간·상태 필터."""
    require_scope(user, "patient.referral", "read")
    return {"items": await _referral().list_referrals(
        session, referral_type=referral_type, start=start, end=end, status=status)}


@router.get("/referrals/stats")
async def referral_stats(start: date, end: date,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """의뢰·회송 실적 — 유형별 건수·의뢰회송료·회송률."""
    require_scope(user, "patient.referral", "read")
    return await _referral().stats(session, start=start, end=end)


@router.post("/referrals/{referral_id}/close")
async def close_referral(referral_id: int,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """의뢰·회송 종결."""
    require_scope(user, "patient.referral", "write")
    r = await _referral().close(session, referral_id=referral_id)
    return {"id": r.id, "status": r.status}


# ── 환자 민원·CS(VOC) — 접수→배정→회신→종결 (Tier3 ①·마이그 0223) ──────────
from erp.patient.voc_service import VocService  # noqa: E402

_voc = VocService()


def _voc_require(user: CurrentUser, action: str):
    return require_scope(user, "patient.voc", action)


class VocReceiveBody(BaseModel):
    title: str
    body: str | None = None
    channel: str = "DESK"
    vtype: str = "COMPLAINT"
    category: str = "ETC"
    severity: str = "LOW"
    patient_id: int | None = None
    contact: str | None = None          # 환자 미연결 시 회신 연락처(AES-GCM 저장)
    department_id: int | None = None


@router.post("/voc", status_code=201)
async def receive_voc(body: VocReceiveBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """민원 접수 — 전 직원(창구 업무). 처리기한 = 접수 + 14일(내부 SLA)."""
    _voc_require(user, "write")
    c = await _voc.receive(
        session, created_by=user.user_id, title=body.title, body=body.body,
        channel=body.channel, vtype=body.vtype, category=body.category,
        severity=body.severity, patient_id=body.patient_id, contact=body.contact,
        department_id=body.department_id if body.department_id is not None else user.dept_id)
    await session.commit()
    return {"id": c.id, "case_no": c.case_no, "due_on": c.due_on.isoformat()}


@router.get("/voc")
async def list_voc(status: str | None = None, vtype: str | None = None,
                   category: str | None = None, overdue: bool = False,
                   user: CurrentUser = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    """민원 목록 — SELF=본인 접수분·DEPT=귀속부서·ALL=전체(행 스코프 합성)."""
    scope = _voc_require(user, "read")
    dept = created_by = None
    if scope.kind == ScopeKind.SELF:
        created_by = user.user_id
    elif scope.kind == ScopeKind.DEPT:
        dept = user.dept_id
    return {"items": await _voc.list_cases(session, status=status, vtype=vtype,
                                           category=category, department_id=dept,
                                           created_by=created_by, overdue=overdue)}


@router.get("/voc/stats")
async def voc_stats(since: date, until: date,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """기간 통계(유형/카테고리/부서·평균 처리일수·기한초과) — 인증평가 자료. 관리 권한."""
    _voc_require(user, "admin")
    return await _voc.stats(session, since=since, until=until)


@router.get("/voc/{case_id}")
async def get_voc(case_id: int,
                  user: CurrentUser = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    scope = _voc_require(user, "read")
    await _voc.assert_row_scope(session, case_id, scope=scope, user=user)
    out = await _voc.get_detail(session, case_id)
    # 민원 본문·연락처=민감 — 상세 열람 접속기록
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="patient.voc_cases", target_id=str(case_id))
    return out


class VocAssignBody(BaseModel):
    assignee_user_id: int
    department_id: int | None = None
    note: str | None = None


@router.post("/voc/{case_id}/assign")
async def assign_voc(case_id: int, body: VocAssignBody,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    scope = _voc_require(user, "admin")
    await _voc.assert_row_scope(session, case_id, scope=scope, user=user)
    c = await _voc.assign(session, case_id=case_id, actor=user.user_id,
                          assignee_user_id=body.assignee_user_id,
                          department_id=body.department_id, note=body.note)
    await session.commit()
    return {"id": c.id, "status": c.status, "assignee_user_id": c.assignee_user_id}


class VocNoteBody(BaseModel):
    note: str


@router.post("/voc/{case_id}/comment")
async def comment_voc(case_id: int, body: VocNoteBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    scope = _voc_require(user, "read")   # 의견은 열람 가능자(담당 실무자 포함)
    await _voc.assert_row_scope(session, case_id, scope=scope, user=user)
    c = await _voc.comment(session, case_id=case_id, actor=user.user_id, note=body.note)
    await session.commit()
    return {"id": c.id, "status": c.status}


class VocAnswerBody(BaseModel):
    answer_body: str


@router.post("/voc/{case_id}/answer")
async def answer_voc(case_id: int, body: VocAnswerBody,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    scope = _voc_require(user, "admin")
    await _voc.assert_row_scope(session, case_id, scope=scope, user=user)
    c = await _voc.answer(session, case_id=case_id, actor=user.user_id,
                          answer_body=body.answer_body)
    await session.commit()
    return {"id": c.id, "status": c.status}


@router.post("/voc/{case_id}/close")
async def close_voc(case_id: int, body: VocNoteBody | None = None,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    scope = _voc_require(user, "admin")
    await _voc.assert_row_scope(session, case_id, scope=scope, user=user)
    c = await _voc.close(session, case_id=case_id, actor=user.user_id,
                         note=body.note if body else None)
    await session.commit()
    return {"id": c.id, "status": c.status}


@router.post("/voc/{case_id}/reopen")
async def reopen_voc(case_id: int, body: VocNoteBody,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    scope = _voc_require(user, "admin")
    await _voc.assert_row_scope(session, case_id, scope=scope, user=user)
    c = await _voc.reopen(session, case_id=case_id, actor=user.user_id, note=body.note)
    await session.commit()
    return {"id": c.id, "status": c.status, "reopen_count": c.reopen_count}


# ── 의료비 연말정산 기초자료 (tax→patient 이관·Phase0 결정 2026-07-23) ────────
@router.get("/medical-expenses")
async def medical_expenses(year: int,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """환자별 연간 수납액(의료비 연말정산 기초) — 세무 리포트지만 환자 PII라 patient 소속."""
    require_scope(user, "finance.statements", "read")
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="patient.medical_expenses", route="/patients/medical-expenses")
    return await _med_expense.annual_by_patient(session, year=year)


@router.get("/medical-expenses.xlsx")
async def medical_expenses_xlsx(year: int,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    from fastapi.responses import StreamingResponse

    from erp.common.excel import build_xlsx
    require_scope(user, "finance.statements", "read")
    out = await _med_expense.annual_by_patient(session, year=year)
    await _audit.record_access(session, actor=user.user_id, action="EXPORT",
                               entity="patient.medical_expenses",
                               route="/patients/medical-expenses.xlsx")
    buf = build_xlsx(sheet_name=f"{year} 의료비",
                     headers=["차트번호", "성명", "연간 수납액(원)"],
                     rows=[[i["chart_no"], i["name"], i["total_paid"]] for i in out["items"]])
    return StreamingResponse(
        iter([buf]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="medical-expenses-{year}.xlsx"'})
