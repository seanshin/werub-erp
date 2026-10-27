"""연동 수신 공통 모델 — HIS 아웃바운드 웹훅 인박스(staging). design §5.2 멱등 수신.

도메인-무관 골격: HIS `*.changed` 웹훅을 도메인별 staging에 멱등 적재 → 도메인 consumer가
후속 처리(Wave별 추가). source_ref UNIQUE = 중복 이벤트 1회 처리.
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON, BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String,
    Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp.db import Base, BigIntPK


class HisInboundEvent(Base):
    """HIS→ERP 웹훅 수신 이벤트 (멱등 인박스) — 도메인별 consumer가 APPLIED 처리."""
    __tablename__ = "his_inbound_events"
    __table_args__ = (
        UniqueConstraint("source_ref", name="uq_his_inbound_source_ref"),
        CheckConstraint("status IN ('NEW','APPLIED','FAILED','DEAD','RESOLVED')",
                        name="ck_his_inbound_status"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(24), index=True)   # finance|materials|assets|hr...
    event_type: Mapped[str] = mapped_column(String(48))           # billing.changed 등
    source_ref: Mapped[str] = mapped_column(String(120))         # event_id 또는 {domain}:{ref}
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(10), default="NEW")  # NEW|APPLIED|FAILED|DEAD|RESOLVED
    retry_count: Mapped[int] = mapped_column(Integer, default=0)    # 일시실패 재처리 횟수
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 재시도 가능 시각
    error: Mapped[str | None] = mapped_column(String(300))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 2026-10-09 시나리오 3회차(0255): DEAD의 운영자 종결(재큐로 못 푸는 가드 DEAD의 출구) — error는 보존.
    resolved_by: Mapped[int | None] = mapped_column(BigInteger)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolve_reason: Mapped[str | None] = mapped_column(String(300))


class EsignFetchOutbox(Base):
    """서명 완료본 회수 아웃박스 (e-sign #6) — sign.completed 미러 시 즉시 enqueue, 별도 잡이 회수.

    완료 미러(_sign_completed)가 15s 네트워크 GET을 인박스 트랜잭션·행잠금 안에서 수행하던 것을 분리
    (전체검토 2026-06-29 Tier2). 미러는 빠른 enqueue만, 회수는 job_esign_fetch가 별도 트랜잭션에서
    수행 → 인박스 배치가 HIS 지연에 묶이지 않음. doc_url(24h 단기 토큰)은 내부 큐에만 보관(DTO 미노출)·
    DONE 후 의미 소멸. (entity_type, contract_id) 고유 = 계약당 1건 멱등.
    """
    __tablename__ = "esign_fetch_outbox"
    __table_args__ = (
        UniqueConstraint("entity_type", "contract_id", name="uq_esign_fetch_outbox"),
        CheckConstraint("status IN ('PENDING','DONE','FAILED','DEAD')",
                        name="ck_esign_fetch_status"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(24))      # medical_contract|outsourcing_contract
    contract_id: Mapped[int] = mapped_column(BigInteger)
    doc_url: Mapped[str] = mapped_column(Text)               # 24h 단기 토큰 URL(내부 큐 한정·DTO 미노출)
    issue_no: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), default="PENDING", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HisStockMirror(Base):
    """HIS 재고 이벤트 미러 (D-4) — ERP Lot/FIFO와 분리 적재. 재고 대사·가시성용.

    HIS stock(CSSD 멸균품)·inventory(범용)는 lot 없는 수량 이벤트라 ERP FIFO(expiry·마약류 무결성)에
    직접 주입하지 않고 별도 기록. lot 매핑 결정(D-4) 후 FIFO 통합 가능.
    """
    __tablename__ = "his_stock_mirror"
    __table_args__ = ({"schema": "integration"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    item_code: Mapped[str] = mapped_column(String(40), index=True)      # erpItemCode 또는 HIS code
    erp_item_code: Mapped[str | None] = mapped_column(String(40))
    name: Mapped[str | None] = mapped_column(String(120))
    movement_type: Mapped[str] = mapped_column(String(12))             # IN|OUT|ADJUST|DISPOSAL...
    qty: Mapped[float] = mapped_column(Numeric(14, 2))                 # 부호 포함
    balance_after: Mapped[float | None] = mapped_column(Numeric(14, 2))
    ref: Mapped[str | None] = mapped_column(String(60))               # PO·LOT 등
    source: Mapped[str] = mapped_column(String(12))                   # CSSD | INVENTORY
    moved_on: Mapped[date] = mapped_column(Date)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PatientRefMap(Base):
    """Phase0 가명 토큰 대사표 (patientRef ↔ chartNo) — HIS 1회성 대사표 이관 결과.

    HIS가 `pseudonymizeChartNo` 플립 후 모든 외부 이벤트의 chartNo 필드를 patientRef
    (=HMAC-SHA256(chartNo, key) hex 앞 16자)로 치환한다. ERP는 이 표로 수신 patientRef를
    원 chart_no로 역해소(inbound_consumer). 결정적 토큰이라 환자당 안정.
    chart_no·patient_ref 양쪽 UNIQUE = 1:1 대사. 원 chart_no는 PHI-bound(원내 로컬)로 보관.
    """
    __tablename__ = "patient_ref_map"
    __table_args__ = (
        UniqueConstraint("chart_no", name="uq_patient_ref_map_chart"),
        UniqueConstraint("patient_ref", name="uq_patient_ref_map_ref"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    chart_no: Mapped[str] = mapped_column(String(20))
    patient_ref: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16), default="HIS_MAP")   # HIS_MAP | INCREMENT
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class PendingPatientRef(Base):
    """미등록 patientRef 보류 큐 (컷오버 후 신규 환자) — 대사표에 없는 patientRef 수신 시 적재.

    컷오버 후 신규 환자는 대사표에 없어 역해소 실패 → 이벤트를 DEAD 처리하지 않고 보류 기록.
    HIS 증분 대사표 재발급 또는 환자생성 이벤트로 resolved_chart_no 채워지면 재처리(후속 협의).
    patient_ref UNIQUE = 동일 미등록 토큰 다건 수신 시 count만 증가.
    """
    __tablename__ = "pending_patient_ref"
    __table_args__ = (
        UniqueConstraint("patient_ref", name="uq_pending_patient_ref"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    patient_ref: Mapped[str] = mapped_column(String(32))
    first_event: Mapped[str | None] = mapped_column(String(64))   # 최초 수신 event_type
    event_count: Mapped[int] = mapped_column(Integer, default=1)
    resolved_chart_no: Mapped[str | None] = mapped_column(String(20))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                onupdate=func.now())


class LisTestEdiMap(Base):
    """LIS 원내 검사코드 ↔ EDI 수가코드 매핑 (규제팀 관리) — LIS 청구 산정의 코드 해소.

    LIS는 원내 testCode만 전송(E-3 A)·ERP가 EDI 수가코드로 매핑해 fee lookup 산정. 실 코드는
    규제팀이 심평원 마스터로 채움(미매핑 검사는 청구 캡처하되 amount 보류=mapped False).
    """
    __tablename__ = "lis_test_edi_map"
    __table_args__ = ({"schema": "integration"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    test_code: Mapped[str] = mapped_column(String(40), unique=True)
    edi_code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str | None] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Integer, default=1)   # 1/0 (sqlite·pg 호환)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LisBillingClaim(Base):
    """LIS 검사 청구 캡처 (E-3 A: LIS 수량·ERP 산정) — 오더 단위 1건(lis_billing_id 멱등).

    revision 재수신 시 대체(정정). status=CAPTURED(수신·캡처)/CANCELLED(취소). mapped=전 항목 EDI
    매핑·산정 완료 여부. amount는 mapped 시 fee lookup(as-of performed_on) 합계, 아니면 None(보류).
    """
    __tablename__ = "lis_billing_claims"
    __table_args__ = (
        UniqueConstraint("lis_billing_id", name="uq_lis_billing_id"),
        CheckConstraint("status IN ('CAPTURED','CANCELLED')", name="ck_lis_claim_status"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    lis_billing_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    chart_no: Mapped[str | None] = mapped_column(String(20))     # 역해소 결과(Phase0 대응)
    visit_id: Mapped[str | None] = mapped_column(String(64))
    performed_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="CAPTURED")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    total_amount: Mapped[float | None] = mapped_column(Numeric(14, 0))
    mapped: Mapped[bool] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class LisBillingClaimItem(Base):
    """LIS 청구 항목 — 검사코드·수량(LIS)·EDI매핑·산정단가/금액(ERP)."""
    __tablename__ = "lis_billing_claim_items"
    __table_args__ = ({"schema": "integration"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("integration.lis_billing_claims.id"),
                                          index=True)
    test_code: Mapped[str] = mapped_column(String(40))
    test_name: Mapped[str | None] = mapped_column(String(120))
    quantity: Mapped[float] = mapped_column(Numeric(10, 2), default=1)
    specimen: Mapped[str | None] = mapped_column(String(20))
    result_status: Mapped[str | None] = mapped_column(String(12))
    edi_code: Mapped[str | None] = mapped_column(String(20))     # 매핑 결과(없으면 보류)
    unit_price: Mapped[float | None] = mapped_column(Numeric(14, 2))
    amount: Mapped[float | None] = mapped_column(Numeric(14, 0))
