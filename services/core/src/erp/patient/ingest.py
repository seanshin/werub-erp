"""HIS 수신 소비 (FR-15/16) — integration 스테이징 → patient 코어.

설계 §5.2: 코어 쓰기는 소비 단계에서만, 기존 서비스(불변식 포함)를 재사용.
- 환자: 차트번호 upsert (FR-33 최소 4필드, 전화는 즉시 암호화)
- 수납: 차트번호 해소 → Reception(source=HIS) → PaymentService.pay
  (마감일이면 FR-54 강제를 위해 FAILED 격리 — 마감 원장 무결성 보존)

행별 savepoint 격리(독성 1행이 배치 전체를 롤백하지 않음)·동시소비 skip_locked(이중적용 방지)·
FAILED 재구동(환자 늦게 도착 시 수납 영구 소실 방지).
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common import crypto
from erp.errors import ValidationError
from erp.patient.models import HISPatientStaging, HISPaymentStaging, Patient
from erp.patient.service import PaymentService, PatientService, ReceptionService

logger = logging.getLogger("erp.patient.ingest")

_patients = PatientService()
_receptions = ReceptionService()
_payments = PaymentService()

# FAILED 후 재구동 가능한 사유 — 환자 스테이징이 수납보다 늦게 도착한 경우(순서 의존)
_RETRYABLE = {"unmapped_chart_no"}


def _mark_failed(row, reason: str) -> None:
    row.status = "FAILED"
    row.error_detail = {"reason": reason, "chart_no": row.chart_no}


def _set_phone(patient: Patient, phone: str | None) -> None:
    if not phone:
        return
    patient.phone_enc = crypto.encrypt_column(
        phone, aad=crypto.column_aad("patient.patients.phone", patient.id))
    patient.phone_bidx = crypto.phone_blind_index(phone)


def _select_consumable(model, limit: int):
    """NEW + 재구동 가능한 FAILED 선택 — 동시 소비 시 skip_locked로 이중적용 차단(sqlite는 no-op)."""
    return (select(model).where(model.status.in_(["NEW", "FAILED"]))
            .order_by(model.id).limit(limit).with_for_update(skip_locked=True))


async def consume_patient_staging(session: AsyncSession, *, limit: int = 5000) -> dict:
    """NEW 환자 스테이징 소비 → 차트번호 upsert → APPLIED. 행별 savepoint 격리·빈 chart 방어."""
    rows = (await session.scalars(_select_consumable(HISPatientStaging, limit))).all()
    # N+1 제거 — 행별 Patient 조회를 chart_no IN 일괄+사전맵으로(10분 주기·최대 5000행·성능검토).
    # 배치 내 동일 chart_no 재등장(create 후 update) 대비 create 시 맵 갱신.
    chart_nos = [r.chart_no for r in rows if (r.chart_no or "").strip()]
    pmap = {p.chart_no: p for p in (await session.scalars(
        select(Patient).where(Patient.chart_no.in_(chart_nos)))).all()} if chart_nos else {}
    created = updated = failed = 0
    for r in rows:
        if r.status == "FAILED":     # 환자 소비는 순서의존 없음 — 재구동 대상 아님(스킵)
            continue
        try:
            async with session.begin_nested():   # 독성 1행 격리(IntegrityError·인코딩 등)
                if not (r.chart_no or "").strip():
                    raise ValidationError("빈 차트번호")
                existing = pmap.get(r.chart_no)
                if existing is None:
                    pmap[r.chart_no] = await _patients.create(
                        session, chart_no=r.chart_no, name=r.name,
                        birth_date=r.birth_date, phone=r.phone)
                    created += 1
                else:
                    existing.name = r.name
                    existing.birth_date = r.birth_date
                    _set_phone(existing, r.phone)
                    updated += 1
                r.status = "APPLIED"
                r.phone = None    # §6.4 — 코어 암호화 반영 후 평문 PII 즉시 파기
        except Exception as exc:  # noqa: BLE001 — poison row 격리(배치 보존)
            _mark_failed(r, getattr(exc, "message", None) or str(exc)[:100])
            r.phone = None        # 실패분도 평문 전화번호 파기(§6.4 — staging 잔존 방지)
            failed += 1
    if rows:
        logger.info("HIS 환자 소비", extra={"ctx": {"created": created, "updated": updated,
                                                 "failed": failed}})
    return {"created": created, "updated": updated, "failed": failed}


async def consume_payment_staging(session: AsyncSession, *, limit: int = 5000) -> dict:
    """NEW 수납 스테이징 소비 → 접수·수납 생성 → APPLIED. 결손/마감은 FAILED 격리·재구동."""
    rows = (await session.scalars(_select_consumable(HISPaymentStaging, limit))).all()
    # N+1 제거 — 행별 Patient 조회를 chart_no IN 일괄+사전맵으로(조회 전용·성능검토)
    chart_nos = [r.chart_no for r in rows if (r.chart_no or "").strip()]
    pmap = {p.chart_no: p for p in (await session.scalars(
        select(Patient).where(Patient.chart_no.in_(chart_nos)))).all()} if chart_nos else {}
    applied = failed = 0
    for r in rows:
        if r.status == "FAILED" and (r.error_detail or {}).get("reason") not in _RETRYABLE:
            continue   # 재구동 불가 FAILED(마감·금액오류)는 스킵 — amend/수기 경로
        patient = pmap.get(r.chart_no)
        if patient is None:
            _mark_failed(r, "unmapped_chart_no")   # 환자 소비 선행 필요 — 다음 회차 재구동
            failed += 1
            continue
        if float(r.amount) <= 0:
            _mark_failed(r, "invalid_amount")
            failed += 1
            continue
        if await _payments._is_closed(session, r.paid_on):
            _mark_failed(r, "day_closed")          # FR-54: 마감 후 정정(amend) 경로로만
            failed += 1
            continue
        try:
            async with session.begin_nested():     # receive+pay 원자 격리(pay 실패 시 유령 Reception 롤백)
                rec = await _receptions.receive(
                    session, patient_id=patient.id, dept_code=r.dept_code or "UNKNOWN",
                    doctor_id=r.doctor_id, received_on=r.paid_on, source="HIS")
                await _payments.pay(session, reception_id=rec.id, amount=float(r.amount),
                                    method=r.method, paid_on=r.paid_on)
                r.status = "APPLIED"
            applied += 1
        except ValidationError as exc:
            _mark_failed(r, getattr(exc, "message", None) or str(exc))
            failed += 1
        except Exception as exc:  # noqa: BLE001 — poison row 격리
            _mark_failed(r, f"unexpected:{type(exc).__name__}")
            failed += 1
    if rows:
        logger.info("HIS 수납 소비", extra={"ctx": {"applied": applied, "failed": failed}})
    return {"applied": applied, "failed": failed}
