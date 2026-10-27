"""patient 스키마 — FR-33 정보 최소화 (이름·생년월일·차트번호·연락처만, 주민번호 미저장)."""
from datetime import date, datetime

from sqlalchemy import (
    JSON, BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, LargeBinary,
    Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp.db import Base, BigIntPK


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = {"schema": "patient"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    chart_no: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    birth_date: Mapped[date] = mapped_column(Date)
    phone_enc: Mapped[bytes | None] = mapped_column(LargeBinary)      # AES-GCM (§6.4)
    phone_bidx: Mapped[str | None] = mapped_column(String(64), index=True)  # blind index
    last_visit_on: Mapped[date | None] = mapped_column(Date)          # 보존연한 5년 기산
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Reception(Base):
    """외래 접수 (FR-16)."""
    __tablename__ = "receptions"
    __table_args__ = {"schema": "patient"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patient.patients.id"), index=True)
    dept_code: Mapped[str] = mapped_column(String(10))
    doctor_id: Mapped[int | None] = mapped_column(BigInteger)
    received_on: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(8), default="MANUAL")  # MANUAL | HIS
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payment(Base):
    """수납 (FR-16) — 마감 후 정정은 reversal로만 (FR-54)."""
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("status IN ('PAID','CANCELED')", name="ck_payment_status"),
        {"schema": "patient"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    reception_id: Mapped[int] = mapped_column(ForeignKey("patient.receptions.id"), index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    method: Mapped[str] = mapped_column(String(10), default="CARD")  # CARD | CASH | TRANSFER
    paid_on: Mapped[date] = mapped_column(Date, index=True)
    care_month: Mapped[str | None] = mapped_column(String(7), index=True)  # 진료월 귀속(마이그 0160)
    status: Mapped[str] = mapped_column(String(10), default="PAID")
    cancels_id: Mapped[int | None] = mapped_column(BigInteger)  # 정정 수납 — 원 수납 참조
    created_by: Mapped[int | None] = mapped_column(BigInteger)   # 입력자(4-eyes) — HIS 인입은 NULL
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Reservation(Base):
    """예약 (FR-17) — 동일 의사·시간대 중복 방지."""
    __tablename__ = "reservations"
    __table_args__ = (
        UniqueConstraint("doctor_id", "reserved_at", name="uq_reservation_slot"),
        CheckConstraint("status IN ('BOOKED','VISITED','NOSHOW','CANCELED')",
                        name="ck_resv_status"),
        {"schema": "patient"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patient.patients.id"), index=True)
    dept_code: Mapped[str] = mapped_column(String(10))
    doctor_id: Mapped[int] = mapped_column(BigInteger)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), default="BOOKED")


class DailyClosing(Base):
    """일 수납 마감 (FR-18) — 마감 시 전표 생성·잠금, 정정은 amend(FR-54)."""
    __tablename__ = "daily_closings"
    __table_args__ = (
        CheckConstraint("status IN ('CLOSED','AMENDED')", name="ck_closing_status"),
        # 일자당 CLOSED는 1건만 — 동시 마감(TOCTOU) 이중 계상 차단(부분 유니크).
        # amend는 원 마감을 AMENDED로 내린 뒤 새 CLOSED를 넣으므로 항상 ≤1 CLOSED.
        Index("uq_closing_active", "closing_on", unique=True,
              postgresql_where=text("status = 'CLOSED'"),
              sqlite_where=text("status = 'CLOSED'")),
        {"schema": "patient"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    closing_on: Mapped[date] = mapped_column(Date, index=True)
    seq: Mapped[int] = mapped_column(default=1)               # 재마감 차수 (원 마감 보존)
    total_amount: Mapped[float] = mapped_column(Numeric(14, 0))
    payment_count: Mapped[int] = mapped_column()
    voucher_ref: Mapped[str | None] = mapped_column(String(64))  # Finance 추적 키 (FK 아님)
    status: Mapped[str] = mapped_column(String(10), default="CLOSED")
    closed_by: Mapped[int] = mapped_column(BigInteger)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HISPatientStaging(Base):
    """HIS 환자 수신 스테이징 (integration 스키마, M0a=REST) — raw 보존·멱등 (design §5.2).

    소비: consume_patient_staging → patient.patients upsert(차트번호 기준).
    source_ref = f"{chart_no}:{source_seen_at}" — 동일 스냅샷 재수집 차단(갱신은 허용)."""
    __tablename__ = "his_patient_staging"
    __table_args__ = (
        UniqueConstraint("source_ref", name="uq_his_patient_source_ref"),
        CheckConstraint("status IN ('NEW','APPLIED','FAILED')", name="ck_his_patient_status"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source_ref: Mapped[str] = mapped_column(String(80))
    chart_no: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(80))
    birth_date: Mapped[date] = mapped_column(Date)
    phone: Mapped[str | None] = mapped_column(String(20))      # 소비 시 즉시 암호화·폐기
    source_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), default="NEW")
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HISPaymentStaging(Base):
    """HIS 수납 수신 스테이징 (integration 스키마) — raw 보존·멱등 (design §5.2).

    소비: consume_payment_staging → 차트번호 해소 → Reception(source=HIS) → PaymentService.pay
    (마감일이면 FR-54 위해 FAILED 격리). source_ref = HIS 수납 식별자(멱등키)."""
    __tablename__ = "his_payment_staging"
    __table_args__ = (
        UniqueConstraint("source_ref", name="uq_his_payment_source_ref"),
        CheckConstraint("status IN ('NEW','APPLIED','FAILED')", name="ck_his_payment_status"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source_ref: Mapped[str] = mapped_column(String(80))
    chart_no: Mapped[str] = mapped_column(String(20), index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    method: Mapped[str] = mapped_column(String(10), default="CARD")
    paid_on: Mapped[date] = mapped_column(Date, index=True)
    dept_code: Mapped[str | None] = mapped_column(String(10))
    doctor_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10), default="NEW")
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BillingLine(Base):
    """진료행위 라인 미러 (D-6) — HIS billing.line_recorded 수신, line_id 멱등 치환.

    ERP 진료비 책정(중간/최종)·사전심사 자동 적재의 원천. cancelled=취소(집계 제외).
    """
    __tablename__ = "billing_lines"
    __table_args__ = (
        CheckConstraint("encounter_type IN ('OUTPATIENT','INPATIENT')", name="ck_bl_enc"),
        CheckConstraint("coverage IN ('NHIS','NONCOVERED','AUTO','INDUSTRIAL','MEDICAL_AID','PATRIOT')",
                        name="ck_bl_coverage"),
        {"schema": "patient"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    line_id: Mapped[str] = mapped_column(String(64), unique=True)
    encounter_id: Mapped[str | None] = mapped_column(String(64), index=True)
    chart_no: Mapped[str] = mapped_column(String(20), index=True)
    encounter_type: Mapped[str] = mapped_column(String(12), default="OUTPATIENT")
    admitted_on: Mapped[date | None] = mapped_column(Date)
    dept_code: Mapped[str | None] = mapped_column(String(10))
    code: Mapped[str] = mapped_column(String(20))
    code_type: Mapped[str] = mapped_column(String(16), default="TARIFF")
    count: Mapped[float] = mapped_column(Numeric(8, 2), default=1)
    treated_on: Mapped[date | None] = mapped_column(Date)
    treated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    patient_age: Mapped[int | None] = mapped_column(Integer)
    patient_sex: Mapped[str | None] = mapped_column(String(1))
    coverage: Mapped[str] = mapped_column(String(12), default="NHIS")
    unit_price_his: Mapped[float | None] = mapped_column(Numeric(14, 2))  # HIS 산정가(대사·비급여 폴백)
    special_case_type: Mapped[str | None] = mapped_column(String(20))  # 산정특례 유형(HIS 동봉·0050)
    medical_aid_class: Mapped[str | None] = mapped_column(String(4))   # 의료급여 종(1/2·HIS 동봉)
    drug_name: Mapped[str | None] = mapped_column(String(200))      # 약품 명칭(매칭 근거·0053)
    drug_ingredient: Mapped[str | None] = mapped_column(String(200))
    ingredient_code: Mapped[str | None] = mapped_column(String(20))   # HIS 주성분코드(DUR 직접·#72)
    pregnancy_status: Mapped[str | None] = mapped_column(String(20))  # 임신상태(임부금기 DUR·#72)
    institution_grade: Mapped[str | None] = mapped_column(String(20))  # HIS config 종별(가산=ERP·#75)
    price_basis: Mapped[str | None] = mapped_column(String(20))       # BASE_CATALOG(기본수가·#75)
    insurance_product_code: Mapped[str | None] = mapped_column(String(20))  # HIS 동봉 보험코드(자격검증·0055)
    item_category: Mapped[str | None] = mapped_column(String(20))   # HIS 항목구분
    diagnosis_codes: Mapped[str | None] = mapped_column(String(120))  # 상병코드(진단·EDI 명세서, 쉼표구분) — 의학적 심사
    eligibility_status: Mapped[str | None] = mapped_column(String(16))  # 자격 검증상태
    his_copay_rate: Mapped[float | None] = mapped_column(Numeric(4, 3))  # HIS 자격기반 본인부담률
    copay_category: Mapped[str | None] = mapped_column(String(20))  # HIS 본인부담 구분(차상위 등·0056)
    cancelled: Mapped[bool] = mapped_column(default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Refund(Base):
    """환불(과오납 반환) — 수납과 분리된 1급 거래(FR-16 확장). 즉시 역(逆)전표 + 추적.

    수납 마감과 독립(즉시 처리). 발생주의 ON이면 DR 환자미수금(1131)/CR 현금, OFF면 DR 외래수익(4110)/CR 현금.
    """
    __tablename__ = "refunds"
    __table_args__ = ({"schema": "patient"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    reception_id: Mapped[int | None] = mapped_column(ForeignKey("patient.receptions.id"), index=True)
    chart_no: Mapped[str] = mapped_column(String(20), index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    method: Mapped[str] = mapped_column(String(10), default="CASH")   # CASH|TRANSFER
    reason: Mapped[str] = mapped_column(String(200))
    refunded_on: Mapped[date] = mapped_column(Date, index=True)
    care_month: Mapped[str | None] = mapped_column(String(7), index=True)  # 진료월 귀속(마이그 0189)
    voucher_ref: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DunningNotice(Base):
    """환자 미수 독촉(납부안내) 이력 — 미수 회수 루프(독촉→분납→상각) 1단계 (마이그 0209).

    receivable_id는 finance.receivables(환자 미러·source_module='patient') 추적 키(FK 아님 —
    voucher_ref 등 기존 크로스모듈 규약). 차수(level)는 채권별 자동 증가(1차/2차/최고장…).
    """
    __tablename__ = "dunning_notices"
    __table_args__ = (
        CheckConstraint("method IN ('SMS','MAIL','PHONE','VISIT')", name="ck_dunning_method"),
        {"schema": "patient"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    receivable_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chart_no: Mapped[str] = mapped_column(String(20), index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)       # 1차·2차·3차(최고)…
    method: Mapped[str] = mapped_column(String(10), default="PHONE")
    outstanding: Mapped[float] = mapped_column(Numeric(14, 0))   # 발송 시점 미수 잔액(스냅샷)
    note: Mapped[str | None] = mapped_column(String(300))
    sent_on: Mapped[date] = mapped_column(Date, index=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstallmentPlan(Base):
    """환자 미수 분납(납부계획) 합의 — 회수 루프 2단계 (마이그 0209).

    합의 시점 잔액(total_amount)을 months 회차로 분납. 수납 자체는 기존 Payment 경로(care_month 귀속)로
    흐르고, 본 엔티티는 합의·추적 기록. 채권당 ACTIVE 1건(부분 유니크).
    """
    __tablename__ = "installment_plans"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','COMPLETED','DEFAULTED','CANCELED')",
                        name="ck_installment_status"),
        Index("uq_installment_active", "receivable_id", unique=True,
              postgresql_where=text("status = 'ACTIVE'"),
              sqlite_where=text("status = 'ACTIVE'")),
        {"schema": "patient"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    receivable_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chart_no: Mapped[str] = mapped_column(String(20), index=True)
    total_amount: Mapped[float] = mapped_column(Numeric(14, 0))   # 합의 시점 미수 잔액
    months: Mapped[int] = mapped_column(Integer)                  # 분납 회차 수
    monthly_amount: Mapped[float] = mapped_column(Numeric(14, 0))
    start_month: Mapped[str] = mapped_column(String(7))           # YYYY-MM
    status: Mapped[str] = mapped_column(String(12), default="ACTIVE")
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VocCase(Base):
    """환자 민원·CS(VOC) — 접수→분류→배정→처리→회신→종결 (Tier3·마이그 0223).

    의료기관 인증평가(민원·고충처리 체계) 소스. 환자 연결은 선택(익명·보호자 접수 허용) —
    비연결 접수의 회신 연락처만 AES-GCM(contact_enc)으로 저장(최소수집).
    """
    __tablename__ = "voc_cases"
    __table_args__ = (
        CheckConstraint("channel IN ('DESK','PHONE','LETTER','WEB','ETC')",
                        name="ck_voc_channel"),
        CheckConstraint("vtype IN ('COMPLAINT','GRIEVANCE','SUGGESTION','PRAISE','INQUIRY')",
                        name="ck_voc_vtype"),
        CheckConstraint("category IN ('CARE','ADMIN','FACILITY','STAFF','COST','PRIVACY','ETC')",
                        name="ck_voc_category"),
        CheckConstraint("severity IN ('LOW','MEDIUM','HIGH')", name="ck_voc_severity"),
        CheckConstraint("status IN ('RECEIVED','IN_PROGRESS','ANSWERED','CLOSED')",
                        name="ck_voc_status"),
        {"schema": "patient"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    case_no: Mapped[str] = mapped_column(String(24), unique=True)     # 민원-YYYY-NNNN
    channel: Mapped[str] = mapped_column(String(10), default="DESK")
    vtype: Mapped[str] = mapped_column(String(12), default="COMPLAINT")
    category: Mapped[str] = mapped_column(String(10), default="ETC")
    severity: Mapped[str] = mapped_column(String(8), default="LOW")   # HIGH=경영진 보고 대상
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patient.patients.id"), index=True)
    contact_enc: Mapped[bytes | None] = mapped_column(LargeBinary)    # AES-GCM (§6.4)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    department_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # 귀속(피민원) 부서
    assignee_user_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(12), default="RECEIVED", index=True)
    received_on: Mapped[date] = mapped_column(Date, index=True)
    due_on: Mapped[date] = mapped_column(Date)                        # 접수+14일(내부 SLA)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answer_body: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    safety_incident_id: Mapped[int | None] = mapped_column(BigInteger)  # regulatory PSI 번호 참조
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VocAction(Base):
    """VOC 처리 이력 — 배정/의견/회신/종결/재오픈 타임라인."""
    __tablename__ = "voc_actions"
    __table_args__ = (
        CheckConstraint("action IN ('ASSIGN','COMMENT','ANSWER','CLOSE','REOPEN')",
                        name="ck_voca_action"),
        {"schema": "patient"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("patient.voc_cases.id"), index=True)
    action: Mapped[str] = mapped_column(String(10))
    actor: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(500))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InterimBilling(Base):
    """입원 중간정산 원장 — HIS billing.interim_recorded/paid 소비 (0224).

    interim_billing_id 멱등 upsert. GL 전기는 게이트(his_interim_gl_enabled) 뒤 —
    보험자분은 기존 claim.submitted(1140) 경로가 인식하므로 본인부담(copay)만 계상.
    """
    __tablename__ = "interim_billings"
    __table_args__ = {"schema": "patient"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    interim_billing_id: Mapped[str] = mapped_column(String(64), unique=True)
    admission_id: Mapped[str | None] = mapped_column(String(64), index=True)
    chart_no: Mapped[str] = mapped_column(String(20), index=True)
    sequence_no: Mapped[int | None] = mapped_column(Integer)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    total_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    insurance_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    copay_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    paid_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12), default="GENERATED")  # GENERATED|PAID|VOID
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # HIS v4.116.0(2026-10-22·0266) — 수납 원장 줄(수단별)·처리자·산정 근거·무효. 과거 수납분은 payments 없음(null).
    payments: Mapped[list | None] = mapped_column(JSON)
    paid_by: Mapped[str | None] = mapped_column(String(64))
    days: Mapped[int | None] = mapped_column(Integer)
    daily_rate: Mapped[float | None] = mapped_column(Numeric(14, 0))
    price_basis: Mapped[str | None] = mapped_column(String(20))   # CODE|CONFIG_BED_CLASS|CONFIG_DAILY|DEFAULT_CONSTANT
    copay_rate: Mapped[float | None] = mapped_column(Numeric(5, 3))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(String(200))


class InterimBillingRefund(Base):
    """입원 중간정산 환불 — HIS billing.interim_refunded 소비(0266). refund_id 멱등·부분 환불 여러 건."""
    __tablename__ = "interim_billing_refunds"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_interim_refund_amount"),
        CheckConstraint("method IN ('CASH','CARD','TRANSFER')", name="ck_interim_refund_method"),
        {"schema": "patient"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    refund_id: Mapped[str] = mapped_column(String(64), unique=True)
    interim_billing_id: Mapped[str] = mapped_column(String(64), index=True)
    chart_no: Mapped[str] = mapped_column(String(20), index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    method: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str | None] = mapped_column(String(200))
    refunded_by: Mapped[str | None] = mapped_column(String(64))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InpatientSettlement(Base):
    """퇴원 최종 정산 대사 스냅샷 — HIS billing.discharge_settlement 소비 (0224).

    값-멱등(dsettle:{admissionId}:{totalCopay}:{alreadyPaid}) 재발행 시 admission_id upsert 대체.
    GL 없음(대사·경고 소스) — 과납/미납 warnings는 재무 알림.
    """
    __tablename__ = "inpatient_settlements"
    __table_args__ = {"schema": "patient"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    admission_id: Mapped[str] = mapped_column(String(64), unique=True)
    chart_no: Mapped[str] = mapped_column(String(20), index=True)
    admit_date: Mapped[date | None] = mapped_column(Date)
    discharge_date: Mapped[date | None] = mapped_column(Date)
    days: Mapped[int | None] = mapped_column(Integer)
    total_charges: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    total_copay: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    already_paid: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    balance_due: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    balanced: Mapped[bool] = mapped_column(default=True)
    overpaid: Mapped[bool] = mapped_column(default=False)
    overpaid_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    unpaid_interims: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[list | None] = mapped_column(JSON)
    breakdown: Mapped[dict | None] = mapped_column(JSON)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Referral(Base):
    """진료의뢰·회송 (진료협력센터·의료법) — 의뢰(타기관 송부)·의뢰접수(초진)·회송(되돌림) 기록.

    referral_type: REFER_OUT(진료의뢰·상급 등 타기관 송부)·REFER_IN(의뢰 접수·초진)·
    BACK_REFER(회송·상급→1차 되돌림). has_fee=의뢰회송료 청구 여부. 회송률 등 실적·인증 지표 소스.
    """
    __tablename__ = "referrals"
    __table_args__ = (
        CheckConstraint("referral_type IN ('REFER_OUT','REFER_IN','BACK_REFER')",
                        name="ck_referral_type"),
        CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_referral_status"),
        {"schema": "patient"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    referral_type: Mapped[str] = mapped_column(String(12), index=True)
    partner_institution: Mapped[str] = mapped_column(String(120))    # 상대 의료기관
    chart_no: Mapped[str | None] = mapped_column(String(20))
    patient_name: Mapped[str | None] = mapped_column(String(80))     # 최소 표시(선택)
    department: Mapped[str | None] = mapped_column(String(40))
    referred_on: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str | None] = mapped_column(String(400))
    has_fee: Mapped[bool] = mapped_column(Integer, default=0)        # 의뢰회송료 청구(1/0)
    status: Mapped[str] = mapped_column(String(10), default="OPEN")
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
