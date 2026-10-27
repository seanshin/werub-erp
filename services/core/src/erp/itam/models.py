"""itam 스키마 — IT 자산관리(ITAM) M1: 하드웨어 대장·배정·벤더 계약.

운영 대장(누가·어디서 보유·생애주기). 회계 감가상각은 finance.FixedAsset이 담당하고
여기서는 finance_asset_id로 연결만 한다(중복 입력 없음·기획 §2).
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer,
    LargeBinary, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp.db import Base, BigIntPK

# 상태: 재고·배정·수리·대여·분실·도난·불용·폐기 (기획 §5-①)
ASSET_STATUSES = ("IN_STOCK", "ASSIGNED", "REPAIR", "LOANER", "LOST", "STOLEN",
                  "RETIRED", "DISPOSED")
CONTRACT_KINDS = ("PURCHASE", "AMC", "SUBSCRIPTION", "SLA")
# 소프트웨어·디지털 서비스 4유형 (기획 §5-③)
SERVICE_STATUSES = ("ACTIVE", "CANCELLED")   # 서비스 운영 상태(해지 = 점검·준수·대시보드 제외)
SERVICE_TYPES = ("PERPETUAL_SW", "SAAS", "AI_SUBSCRIPTION", "CLOUD", "OPEN_SOURCE")
BILLING_MODELS = ("ONE_TIME", "SEAT_RECURRING", "USAGE_METERED")
BILLING_CYCLES = ("ONE_TIME", "MONTHLY", "ANNUAL", "USAGE")
LICENSE_METRICS = ("PER_SEAT", "PER_CORE", "CAL", "CONCURRENT")
PAYMENT_TERMS = ("PREPAID", "POSTPAID", "FREE_TRIAL")   # 선불·후불·무료기간


class VendorContract(Base):
    """벤더 계약 — 도입(구매)/유지보수(AMC)/정기(구독)/SLA. 자산·서비스가 참조(다대일). 계약서는 첨부(itam_contract).

    관리 항목: 체결·발효·만기·차기검토 일자, 상대/내부 담당자·부서, 사유·주요사항.
    """
    __tablename__ = "vendor_contracts"
    __table_args__ = {"schema": "itam"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    vendor: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(12), default="PURCHASE")
    title: Mapped[str] = mapped_column(String(160))
    signed_on: Mapped[date | None] = mapped_column(Date)      # 계약 체결(서명)일자
    start_on: Mapped[date | None] = mapped_column(Date)       # 발효(개시)일
    end_on: Mapped[date | None] = mapped_column(Date)         # 만기 → 컴플라이언스 대시보드
    next_review_on: Mapped[date | None] = mapped_column(Date)  # 차기 검토·갱신 검토 시기(정기/유지보수)
    amount: Mapped[float | None] = mapped_column(Numeric(16, 0))
    # 상대(벤더) 담당자
    counterparty: Mapped[str | None] = mapped_column(String(80))          # 상대 담당자명
    counterparty_contact: Mapped[str | None] = mapped_column(String(120))  # 연락처·이메일
    # 내부 관리 주체
    owner_employee_id: Mapped[int | None] = mapped_column(BigInteger)     # 내부 담당자
    department_id: Mapped[int | None] = mapped_column(BigInteger)         # 담당 부서
    reason: Mapped[str | None] = mapped_column(Text)         # 도입/계약 사유
    sla_note: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)           # 비고·주요 사항
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ITAsset(Base):
    """IT 하드웨어 자산 — 운영 대장. asset_tag(QR/바코드)로 실사 스캔."""
    __tablename__ = "it_assets"
    __table_args__ = (
        CheckConstraint(f"status IN {ASSET_STATUSES}", name="ck_itasset_status"),
        {"schema": "itam"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    asset_tag: Mapped[str] = mapped_column(String(40), unique=True)       # QR/바코드 태그
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(16), default="LAPTOP")   # LAPTOP/DESKTOP/SERVER/…
    serial_no: Mapped[str | None] = mapped_column(String(80), unique=True)
    model: Mapped[str | None] = mapped_column(String(80))
    manufacturer: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(10), default="IN_STOCK")
    owner_department_id: Mapped[int | None] = mapped_column(BigInteger)   # 소유/위치 부서
    assigned_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr.employees.id"))                                   # 현재 사용자
    location: Mapped[str | None] = mapped_column(String(120))
    acquired_on: Mapped[date | None] = mapped_column(Date)
    warranty_until: Mapped[date | None] = mapped_column(Date)             # 보증 만기 → 대시보드
    # 네트워크 1급 필드 (중복 IP 탐지·쿼리)
    ip_addr: Mapped[str | None] = mapped_column(String(45))
    hostname: Mapped[str | None] = mapped_column(String(80))
    mac_addr: Mapped[str | None] = mapped_column(String(17))
    # 보안/PHI (개인정보 안전성 확보조치 감사)
    handles_phi: Mapped[bool] = mapped_column(Boolean, default=False)
    phi_ever: Mapped[bool] = mapped_column(Boolean, default=False)   # 한 번이라도 PHI → sticky(소거 게이트 회피 방지)
    security_grade: Mapped[str | None] = mapped_column(String(8))
    disk_encrypted: Mapped[bool | None] = mapped_column(Boolean)
    av_installed: Mapped[bool | None] = mapped_column(Boolean)
    # 의료기기 (비방사선 의료 IT)
    udi: Mapped[str | None] = mapped_column(String(60))
    mfds_no: Mapped[str | None] = mapped_column(String(40))
    calibration_due: Mapped[date | None] = mapped_column(Date)
    # 예정일(정비·회수·정기점검) — 대시보드/알림 (요청)
    next_inspection_on: Mapped[date | None] = mapped_column(Date)         # 정기점검 예정일
    next_maintenance_on: Mapped[date | None] = mapped_column(Date)        # 정비 예정일
    return_due_on: Mapped[date | None] = mapped_column(Date)              # 회수 예정일(대여·임시)
    spec: Mapped[dict | None] = mapped_column(JSON)                       # 장비 디테일(CPU/RAM/디스크 등)
    procurement_po_line_id: Mapped[int | None] = mapped_column(BigInteger)  # 조달 입고 연계(scm.po_lines·값링크·R4)
    # 취득유형(0227) — OWNED(구매·자체)=finance_asset_id, LEASED(리스)=lease_id. 상호 배타.
    acquisition_type: Mapped[str] = mapped_column(String(10), default="OWNED")
    finance_asset_id: Mapped[int | None] = mapped_column(BigInteger)      # FK→finance.FixedAsset(연결만)
    lease_id: Mapped[int | None] = mapped_column(BigInteger)              # →finance.LeaseContract(값링크·IFRS16)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("itam.vendor_contracts.id"))
    last_audited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # QR 실사 확인 시각(F-9)
    # 폐기·데이터 소거(M3·G-7/G-12) — PHI 장비는 소거 확인 후에만 폐기 가능
    data_erased: Mapped[bool | None] = mapped_column(Boolean)
    disposed_at: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AssetAssignment(Base):
    """자산 배정 이력 — open(returned_on 없음) 행이 현재 보유. 수령확인(chain-of-custody)."""
    __tablename__ = "asset_assignments"
    __table_args__ = {"schema": "itam"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("itam.it_assets.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"))
    assigned_on: Mapped[date] = mapped_column(Date)
    returned_on: Mapped[date | None] = mapped_column(Date)
    assigned_by: Mapped[int | None] = mapped_column(BigInteger)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 수령 확인
    return_condition: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)


class SoftwareService(Base):
    """소프트웨어·디지털 서비스 마스터 — 구매SW·SaaS·AI구독·클라우드를 유형 필드로 유형화 (M2·기획 §5-③)."""
    __tablename__ = "software_services"
    __table_args__ = (
        CheckConstraint(f"service_type IN {SERVICE_TYPES}", name="ck_svc_type"),
        CheckConstraint(f"billing_model IN {BILLING_MODELS}", name="ck_svc_billing"),
        {"schema": "itam"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    vendor: Mapped[str | None] = mapped_column(String(120))
    edition: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE")   # ACTIVE | CANCELLED(해지)
    cancelled_on: Mapped[date | None] = mapped_column(Date)
    service_type: Mapped[str] = mapped_column(String(16), default="SAAS")
    billing_model: Mapped[str] = mapped_column(String(16), default="SEAT_RECURRING")
    billing_cycle: Mapped[str | None] = mapped_column(String(10))
    seats: Mapped[int | None] = mapped_column(Integer)                    # 좌석형만
    license_metric: Mapped[str | None] = mapped_column(String(12))        # PER_SEAT/PER_CORE/CAL/…
    alloc_basis: Mapped[str | None] = mapped_column(String(8))            # USER | DEVICE
    unit_cost: Mapped[float | None] = mapped_column(Numeric(16, 2))
    recurring_cost: Mapped[float | None] = mapped_column(Numeric(16, 2))
    currency: Mapped[str] = mapped_column(String(3), default="KRW")       # USD 구독 대응
    account_ref: Mapped[str | None] = mapped_column(String(120))          # 벤더 계정/테넌트
    portal_url: Mapped[str | None] = mapped_column(String(200))           # 관리 콘솔 URL
    domain: Mapped[str | None] = mapped_column(String(120))               # 서비스 도메인
    homepage_url: Mapped[str | None] = mapped_column(String(200))         # 홈페이지·관련 링크
    api_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)      # API 토큰(AES-GCM)
    tech_info: Mapped[str | None] = mapped_column(Text)                   # IT 정보(리전·티어·리소스 등)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("itam.vendor_contracts.id"))
    owner_employee_id: Mapped[int | None] = mapped_column(BigInteger)     # 서비스 담당자
    sales_contact: Mapped[str | None] = mapped_column(String(120))        # 영업 담당자
    cost_center: Mapped[str | None] = mapped_column(String(40))
    cost_period: Mapped[str | None] = mapped_column(String(8))            # MONTHLY | ANNUAL(예상비용 단위)
    vat_included: Mapped[bool | None] = mapped_column(Boolean)            # 부가세 포함 여부
    usage_unit: Mapped[str | None] = mapped_column(String(16))            # 결제 단위(토큰·vCPU-h 등)
    registered_on: Mapped[date | None] = mapped_column(Date)              # 등록일자(시스템 기록일)
    started_on: Mapped[date | None] = mapped_column(Date)                 # 개시일자(서비스 사용 개시)
    first_billing_on: Mapped[date | None] = mapped_column(Date)           # 유료 첫 결제일
    payment_terms: Mapped[str | None] = mapped_column(String(10))         # PREPAID/POSTPAID/FREE_TRIAL
    expires_on: Mapped[date | None] = mapped_column(Date)                 # 구독/유지보수 만기 → 대시보드
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_notice_days: Mapped[int | None] = mapped_column(Integer)       # 해지 통보 필요일수
    license_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary)    # AES-GCM(구매 SW 키)
    intangible_asset_id: Mapped[int | None] = mapped_column(BigInteger)   # FK→finance.IntangibleAsset(연결만)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ServiceAllocation(Base):
    """좌석 배정(좌석형 서비스) — open(released_on 없음) 행이 현재 사용. 준수율=open vs seats (M2·G-4)."""
    __tablename__ = "service_allocations"
    __table_args__ = {"schema": "itam"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("itam.software_services.id"), index=True)
    it_asset_id: Mapped[int | None] = mapped_column(ForeignKey("itam.it_assets.id"))
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("hr.employees.id"))
    allocated_on: Mapped[date] = mapped_column(Date)
    released_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)


class ServiceCost(Base):
    """구독·클라우드 월 비용 원장(M2b) — 정기료 + 사용량 변동비. USD 청구는 KRW 환산 보관."""
    __tablename__ = "service_costs"
    __table_args__ = (
        UniqueConstraint("service_id", "period", name="uq_service_cost_period"),
        {"schema": "itam"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("itam.software_services.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))               # yyyy-mm
    amount: Mapped[float] = mapped_column(Numeric(16, 2))        # 청구 통화 금액
    currency: Mapped[str] = mapped_column(String(3), default="KRW")
    fx_rate: Mapped[float | None] = mapped_column(Numeric(12, 4))
    amount_krw: Mapped[float] = mapped_column(Numeric(16, 0))    # KRW 환산(집계·회계용)
    usage_note: Mapped[str | None] = mapped_column(String(200))  # vCPU-h·토큰·GB 등
    source: Mapped[str] = mapped_column(String(10), default="MANUAL")  # MANUAL/INVOICE/API
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # GL 전표 전기 시각(R4·F-6)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CostReview(Base):
    """비용 주기 점검·변동 감지(M2b·G-9) — 월별 서비스 비용 이상/미입력을 플래그·책임자 확인."""
    __tablename__ = "cost_reviews"
    __table_args__ = (
        UniqueConstraint("service_id", "period", name="uq_cost_review_period"),
        {"schema": "itam"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("itam.software_services.id"), index=True)
    prev_amount: Mapped[float | None] = mapped_column(Numeric(16, 0))
    amount: Mapped[float | None] = mapped_column(Numeric(16, 0))
    delta_pct: Mapped[float | None] = mapped_column(Numeric(8, 2))
    reason: Mapped[str | None] = mapped_column(String(10))       # SPIKE/DROP/MISSING
    status: Mapped[str] = mapped_column(String(10), default="PENDING")  # PENDING/FLAGGED/CONFIRMED
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class ITBudget(Base):
    """IT 비용 예산(R4·F-6) — 월/연 예산을 코스트센터별·전체로 설정, 실적(service_costs) 대비 초과 감지.

    회계 예산(finance)과 별개인 IT 운영 예산 관리용. cost_center NULL = 기관 IT 총예산.
    """
    __tablename__ = "it_budgets"
    __table_args__ = {"schema": "itam"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(7))              # yyyy(연) 또는 yyyy-mm(월)
    cost_center: Mapped[str | None] = mapped_column(String(40))  # NULL = 전체
    amount_krw: Mapped[float] = mapped_column(Numeric(16, 0))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


OFFBOARDING_STATUSES = ("IN_PROGRESS", "COMPLETED")


class Offboarding(Base):
    """IT 퇴사 회수 기록(M4 확대) — 검토자·인수인계자·비고·실물 사진(itam_offboarding 첨부)로 관리."""
    __tablename__ = "offboardings"
    __table_args__ = {"schema": "itam"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    reviewer_id: Mapped[int | None] = mapped_column(BigInteger)       # 검토자
    handover_to_id: Mapped[int | None] = mapped_column(BigInteger)    # 인수인계자
    status: Mapped[str] = mapped_column(String(12), default="IN_PROGRESS")
    note: Mapped[str | None] = mapped_column(Text)
    completed_on: Mapped[date | None] = mapped_column(Date)
    completed_by: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MaintenanceRecord(Base):
    """유지보수 이력(M3·G-6) — 수리·정기점검·업그레이드·교정. 증빙 첨부(itam_maintenance)."""
    __tablename__ = "maintenance_records"
    __table_args__ = {"schema": "itam"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    it_asset_id: Mapped[int] = mapped_column(ForeignKey("itam.it_assets.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12), default="REPAIR")   # REPAIR/INSPECTION/UPGRADE/CALIBRATION
    on_date: Mapped[date] = mapped_column(Date)
    vendor: Mapped[str | None] = mapped_column(String(120))
    cost: Mapped[float | None] = mapped_column(Numeric(16, 0))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
