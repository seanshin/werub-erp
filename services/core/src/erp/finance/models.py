"""finance 스키마 — design §3.5. 전표는 VoucherService 경유만 (모듈 경계 원칙 ②)."""
from datetime import date, datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp.common.months import ym_check_sql
from erp.db import Base, BigIntPK


class Account(Base):
    """계정과목 (코드 트리)."""
    __tablename__ = "accounts"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10))  # REVENUE|EXPENSE|ASSET|LIABILITY|CAPITAL
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("finance.accounts.id"))
    active: Mapped[bool] = mapped_column(default=True)   # 비활성=신규 사용 차단(과거 집계 유지)
    cost_type: Mapped[str | None] = mapped_column(String(10))  # FIXED|VARIABLE|None — 대시보드 고정비 분류
    cost_function: Mapped[str | None] = mapped_column(String(6))  # COGS|SGA|OTHER — 기능별 손익(IAS 1)
    # IFRS element 데이터화(G8 Phase0) — 재무제표 분류를 코드 하드코딩에서 계정마스터로 이관.
    is_current: Mapped[bool | None] = mapped_column()  # 자산/부채 유동 여부(BS 분류·NULL=미분류/무관)
    cf_activity: Mapped[str | None] = mapped_column(String(10))  # OPERATING|INVESTING|FINANCING(현금흐름)
    # G10 기초(0231) — 표준 IFRS/K-IFRS 택소노미 concept(예: ifrs-full:Revenue). XBRL element 태깅.
    ifrs_element: Mapped[str | None] = mapped_column(String(80))


class Voucher(Base):
    """전표 — (source_module, source_ref) UNIQUE = 멱등키 (design §4.4)."""
    __tablename__ = "vouchers"
    __table_args__ = (
        UniqueConstraint("source_module", "source_ref", name="uq_voucher_source"),
        # M2 승인 워크플로 — 수동 전표 DRAFT→PENDING→POSTED(승인)|REJECTED. 자동전표는 즉시 POSTED.
        CheckConstraint("status IN ('DRAFT','PENDING','POSTED','REJECTED','REVERSED')",
                        name="ck_voucher_status"),
        # 집계 핫패스 인덱스(마이그 0120) — 재무제표 범위필터 + 감사/마감 status 동반필터.
        Index("ix_voucher_on", "voucher_on"),
        Index("ix_voucher_status_on", "status", "voucher_on"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    voucher_on: Mapped[date] = mapped_column(Date)
    source_module: Mapped[str] = mapped_column(String(16))   # patient | claims | scm | manual
    source_ref: Mapped[str] = mapped_column(String(64))      # 출처 추적 키 (FK 아님)
    memo: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="POSTED")
    reversed_by: Mapped[int | None] = mapped_column(BigInteger)  # 역분개 전표 id (FR-54)
    reverses: Mapped[int | None] = mapped_column(BigInteger)     # 원 전표 id
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # M2 maker-checker — DRAFT/PENDING 전표는 원장 라인 미생성(draft_lines 보관), 승인 시 전기.
    submitted_by: Mapped[int | None] = mapped_column(BigInteger)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    draft_lines: Mapped[list | None] = mapped_column(JSON)   # 승인 전 분개안(미전기)


class VoucherLine(Base):
    __tablename__ = "voucher_lines"
    __table_args__ = (
        CheckConstraint("side IN ('DEBIT','CREDIT')", name="ck_line_side"),
        # 계정별 GL 잔액 집계(IFRS/결산 엔진 9종·재무제표)가 account_code로 필터 → 무인덱스 시
        # 최대 성장 테이블 풀스캔. (account_code, voucher_id) 복합으로 필터+vouchers 조인 커버.
        Index("ix_voucher_lines_account", "account_code", "voucher_id"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    voucher_id: Mapped[int] = mapped_column(ForeignKey("finance.vouchers.id"), index=True)
    account_code: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(6))
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    department_id: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[str | None] = mapped_column(String(200))
    # 라인 확장 차원(G9 Phase0) — department_id와 직교한 관리회계 차원(부문/프로젝트 리포팅 정밀화).
    cost_center: Mapped[str | None] = mapped_column(String(20))   # 코스트센터(관리회계 단위)
    project: Mapped[str | None] = mapped_column(String(30))        # 프로젝트 코드


class Budget(Base):
    """부서·연도·계정과목별 예산 (FR-09) — 집행 집계는 서비스 쿼리 (뷰 직접 접근 금지)."""
    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint("department_id", "fiscal_year", "account_code", name="uq_budget"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    department_id: Mapped[int] = mapped_column(BigInteger)
    fiscal_year: Mapped[int] = mapped_column()
    account_code: Mapped[str] = mapped_column(String(20))
    amount: Mapped[float] = mapped_column(Numeric(14, 0))


class FixedAsset(Base):
    """고정자산·의료장비 (FR-09 확장) — 감가상각은 전표 자동생성으로 재무제표 반영."""
    __tablename__ = "fixed_assets"
    __table_args__ = (
        CheckConstraint("category IN ('MEDICAL','FIXTURE','BUILDING','VEHICLE','OTHER')",
                        name="ck_asset_category"),
        CheckConstraint("method IN ('STRAIGHT','DECLINING')", name="ck_asset_method"),
        CheckConstraint("status IN ('IN_USE','DISPOSED')", name="ck_asset_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    asset_no: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(12), default="MEDICAL")
    acquired_on: Mapped[date] = mapped_column(Date)
    acquisition_cost: Mapped[float] = mapped_column(Numeric(16, 0))
    salvage_value: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    useful_life_years: Mapped[int] = mapped_column()
    method: Mapped[str] = mapped_column(String(10), default="STRAIGHT")
    department_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10), default="IN_USE")
    accumulated_depreciation: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    accumulated_impairment: Mapped[float] = mapped_column(Numeric(16, 0), default=0)  # 손상누계
    accumulated_revaluation: Mapped[float] = mapped_column(Numeric(16, 0), default=0)  # 재평가누계(±)
    fair_value_level: Mapped[str | None] = mapped_column(String(1))  # 공정가치 서열 1|2|3 (K-IFRS 1113)
    disposed_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DepreciationEntry(Base):
    """월 감가상각 내역 — 자산×기간 멱등(전표 1회 생성)."""
    __tablename__ = "depreciation_entries"
    __table_args__ = (
        UniqueConstraint("asset_id", "period", name="uq_depreciation_period"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("finance.fixed_assets.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))            # yyyymm
    amount: Mapped[float] = mapped_column(Numeric(16, 0))
    book_value_after: Mapped[float] = mapped_column(Numeric(16, 0))
    voucher_ref: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Receivable(Base):
    """미수금(채권) — 발생 시 미수금/수익, 수금 시 현금/미수금 전표.

    WRITTEN_OFF(개별 대손상각·마이그 0209) = 회수 불능 확정. 충당금(1190) 상계 우선·부족분
    대손상각비(5690). aging·ECL 대상에서 제외(상각일 이후)."""
    __tablename__ = "receivables"
    __table_args__ = (
        CheckConstraint("status IN ('OUTSTANDING','PARTIAL','COLLECTED','WRITTEN_OFF')",
                        name="ck_ar_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    party: Mapped[str] = mapped_column(String(120))          # 거래처/보험자/환자
    description: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    collected_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    billed_on: Mapped[date] = mapped_column(Date)
    due_on: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(12), default="OUTSTANDING")
    source_module: Mapped[str] = mapped_column(String(16), default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(64))
    written_off_on: Mapped[date | None] = mapped_column(Date)          # 개별 상각일(0209)
    write_off_reason: Mapped[str | None] = mapped_column(String(200))  # 상각 사유
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payable(Base):
    """미지급금(채무) — 발생 시 비용/미지급, 지급 시 미지급/현금 전표."""
    __tablename__ = "payables"
    __table_args__ = (
        CheckConstraint("status IN ('OUTSTANDING','PARTIAL','PAID')", name="ck_ap_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    party: Mapped[str] = mapped_column(String(120))          # 공급사/거래처
    description: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    paid_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    incurred_on: Mapped[date] = mapped_column(Date)
    due_on: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(12), default="OUTSTANDING")
    source_module: Mapped[str] = mapped_column(String(16), default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(64))
    # 거래처 마스터 안정 연계키(마이그 0216) — 예 "supplier:123". 이름(party)만이던 그룹핑을 보강
    # (동명 거래처·개명에도 정확). finance는 scm import 불가라 FK 아닌 문자열 참조(voucher_ref 관례).
    party_ref: Mapped[str | None] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CostDriverValue(Base):
    """원가동인 값 (FR-42 ABC) — 기간×원가대상×동인별 가중치. HEADCOUNT는 자동(미저장)."""
    __tablename__ = "cost_driver_values"
    __table_args__ = (
        UniqueConstraint("period", "department_id", "driver", name="uq_driver_value"),
        CheckConstraint(ym_check_sql("period"), name="ck_driver_value_period"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(7))           # yyyymm
    department_id: Mapped[int] = mapped_column(BigInteger)
    driver: Mapped[str] = mapped_column(String(16))          # CASES | AREA | USAGE ...
    value: Mapped[float] = mapped_column(Numeric(14, 2))


class FiscalPeriod(Base):
    """회계 기간 마감 (월 결산) — CLOSED면 해당 월 전표 생성 차단. 마감 시 손익 스냅샷 보존."""
    __tablename__ = "fiscal_periods"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_period_status"),
        {"schema": "finance"},
    )

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)   # 2026-06
    fiscal_year: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(8), default="OPEN")     # OPEN | CLOSED
    total_revenue: Mapped[float | None] = mapped_column(Numeric(16, 2))
    total_expense: Mapped[float | None] = mapped_column(Numeric(16, 2))
    net_income: Mapped[float | None] = mapped_column(Numeric(16, 2))
    voucher_count: Mapped[int | None] = mapped_column(Integer)
    closed_by: Mapped[int | None] = mapped_column(BigInteger)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(200))
    # 재개 추적(내부통제 G2) — 재개가 마감 귀속(closed_by/at)을 파괴하지 않고 별도 이력 보존.
    reopened_by: Mapped[int | None] = mapped_column(BigInteger)
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopen_note: Mapped[str | None] = mapped_column(String(200))
    reopen_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Encumbrance(Base):
    """예산 약정 (encumbrance) — 발주승인 등 지출예정 예약. OPEN=가용예산 차감, 집행 시 RELEASED."""
    __tablename__ = "encumbrances"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','RELEASED')", name="ck_enc_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    department_id: Mapped[int] = mapped_column(BigInteger)
    fiscal_year: Mapped[int] = mapped_column(Integer)
    account_code: Mapped[str] = mapped_column(String(20))
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    description: Mapped[str | None] = mapped_column(String(200))
    source_ref: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), default="OPEN")
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BudgetAmendment(Base):
    """예산 변경 이력 — 증액·감액 감사 추적 (누가·언제·사유·금액 변화)."""
    __tablename__ = "budget_amendments"
    __table_args__ = ({"schema": "finance"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    department_id: Mapped[int] = mapped_column(BigInteger)
    fiscal_year: Mapped[int] = mapped_column(Integer)
    account_code: Mapped[str] = mapped_column(String(20))
    old_amount: Mapped[float | None] = mapped_column(Numeric(14, 0))
    new_amount: Mapped[float] = mapped_column(Numeric(14, 0))
    reason: Mapped[str | None] = mapped_column(String(200))
    changed_by: Mapped[int | None] = mapped_column(BigInteger)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CostAllocationSnapshot(Base):
    """원가 배부 스냅샷 (마감 기준) — 기간×부서별 배부원가 확정 보존(재현·진료과 손익)."""
    __tablename__ = "cost_allocation_snapshots"
    __table_args__ = (
        UniqueConstraint("period", "department_id", name="uq_cost_snap"),
        CheckConstraint(ym_check_sql("period"), name="ck_cost_snap_period"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(7))           # yyyymm
    department_id: Mapped[int] = mapped_column(BigInteger)
    department: Mapped[str | None] = mapped_column(String(80))
    labor: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    material: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    overhead: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    total_cost: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeaseContract(Base):
    """리스계약 (K-IFRS 16) — 사용권자산·리스부채. 매월 이자·원금상환·상각 누적."""
    __tablename__ = "lease_contracts"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','ENDED')", name="ck_lease_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    lease_no: Mapped[str] = mapped_column(String(30), unique=True)
    asset_name: Mapped[str] = mapped_column(String(120))
    lessor: Mapped[str | None] = mapped_column(String(120))
    start_on: Mapped[date] = mapped_column(Date)
    term_months: Mapped[int] = mapped_column(Integer)
    monthly_payment: Mapped[float] = mapped_column(Numeric(16, 0))
    annual_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0)
    rou_initial: Mapped[float] = mapped_column(Numeric(16, 0))
    liability_initial: Mapped[float] = mapped_column(Numeric(16, 0))
    current_liability: Mapped[float] = mapped_column(Numeric(16, 0))
    accumulated_amort: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    periods_accrued: Mapped[int] = mapped_column(Integer, default=0)
    exempt: Mapped[bool] = mapped_column(Boolean, default=False)  # 단기·소액리스 면제(K-IFRS 1116)
    status: Mapped[str] = mapped_column(String(8), default="ACTIVE")
    # IT자산 리스 연결(0227) — GENERAL(일반)/IT(itam.it_assets 리스취득). itam_asset_id 백링크.
    asset_kind: Mapped[str] = mapped_column(String(10), default="GENERAL")
    itam_asset_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeaseAccrual(Base):
    """리스 월별 회계처리 — 이자·원금·상각 (멱등: 계약×기간)."""
    __tablename__ = "lease_accruals"
    __table_args__ = (
        UniqueConstraint("lease_id", "period", name="uq_lease_accrual"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("finance.lease_contracts.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))
    interest: Mapped[float] = mapped_column(Numeric(16, 0))
    principal: Mapped[float] = mapped_column(Numeric(16, 0))
    amortization: Mapped[float] = mapped_column(Numeric(16, 0))
    voucher_ref: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaxInvoice(Base):
    """전자세금계산서 수취·대사 (A-1) — 지급 전 증빙 통제.

    TAX=세금계산서(과세) | EXEMPT=계산서(면세) | ZERO=영세율. 의료기관은 면세사업
    중심이라 매입 VAT 원칙 불공제(원가) — 과세/면세 집계가 A-2(신고)의 기초.
    """
    __tablename__ = "tax_invoices"
    __table_args__ = (
        CheckConstraint("invoice_type IN ('TAX','EXEMPT','ZERO')", name="ck_ti_type"),
        CheckConstraint("direction IN ('IN','OUT')", name="ck_ti_dir"),
        CheckConstraint("status IN ('RECEIVED','MATCHED','VOID')", name="ck_ti_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    approval_no: Mapped[str] = mapped_column(String(30), unique=True)   # 국세청 승인번호
    invoice_type: Mapped[str] = mapped_column(String(8), default="TAX")
    direction: Mapped[str] = mapped_column(String(3), default="IN")
    issue_date: Mapped[date] = mapped_column(Date, index=True)
    supplier_bizno: Mapped[str] = mapped_column(String(13))
    supplier_name: Mapped[str | None] = mapped_column(String(120))
    supply_amount: Mapped[float] = mapped_column(Numeric(14, 0))
    vat_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    total_amount: Mapped[float] = mapped_column(Numeric(14, 0))
    status: Mapped[str] = mapped_column(String(10), default="RECEIVED", index=True)
    payable_id: Mapped[int | None] = mapped_column(ForeignKey("finance.payables.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClosingSnapshot(Base):
    """결산 마감 확정 스냅샷 (M3) — 마감 시 재무제표 4종 JSON 보존(사후 변경 감사)."""
    __tablename__ = "closing_snapshots"
    __table_args__ = (
        # 활성(미대체) 스냅샷만 (yyyymm,kind) 유일. 재개 시 supersede→재마감 시 새 버전 append
        # (무버전 덮어쓰기 방지·확정 재무제표 이력 보존·G2).
        Index("uq_closing_snapshot_active", "yyyymm", "kind", unique=True,
              sqlite_where=text("superseded_at IS NULL"),
              postgresql_where=text("superseded_at IS NULL")),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    yyyymm: Mapped[str] = mapped_column(String(7), index=True)
    kind: Mapped[str] = mapped_column(String(20))   # trial_balance|income|balance_sheet|cash_flow
    payload: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LedgerIntegrityCheckpoint(Base):
    """원장 무결성 체크포인트(G11) — voucher_lines(append-only)의 증분 세그먼트를 해시 체인화.

    각 체크포인트는 직전 체크포인트 이후 신규 라인(id 증가)만 커버하고, content_hash=SHA-256(prev_hash ‖
    세그먼트 라인 정규표현)으로 연결한다. 주기 검증 잡이 재계산 대조 → 트리거/REVOKE를 우회하는
    superuser 변조(session_replication_role·직접 DELETE/UPDATE)까지 사후 탐지. 테이블 자체는 append-only(REVOKE)."""
    __tablename__ = "ledger_integrity"
    __table_args__ = ({"schema": "finance"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    up_to_line_id: Mapped[int] = mapped_column(BigInteger)   # 이 체크포인트가 커버하는 최대 voucher_line id
    line_count: Mapped[int] = mapped_column(BigInteger)      # 누적 라인 수
    sum_debit: Mapped[int] = mapped_column(Numeric(18, 0))   # 누적 차변
    sum_credit: Mapped[int] = mapped_column(Numeric(18, 0))  # 누적 대변
    content_hash: Mapped[str] = mapped_column(String(64))    # SHA-256(prev_hash ‖ 세그먼트)
    prev_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VoucherPostingRule(Base):
    """검진권 거래 회계 전기 규칙 (관리자 설정) — lineType[+딜러유형] → 차·대 계정.

    수수료 방향(비용/수익)·정산(AP/AR)·딜러유형별 처리가 기관·계약마다 달라 하드코딩 대신
    데이터 규칙으로 관리(확장 가능). dealer_type=NULL은 전체 적용, 특정 딜러유형 규칙이 우선.
    skip=무전표(resale 등), reverse_on_negative=음수금액 차대반전(수수료 clawback).
    """
    __tablename__ = "voucher_posting_rules"
    __table_args__ = (
        CheckConstraint(
            "line_type IN ('purchase','sub_purchase','resale','redeem','topup','refund',"
            "'commission','settlement')", name="ck_vrule_line"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    line_type: Mapped[str] = mapped_column(String(16), index=True)
    dealer_type: Mapped[str | None] = mapped_column(String(12))   # DOMESTIC|OVERSEAS|NULL=전체
    debit_account: Mapped[str | None] = mapped_column(String(20))   # 차변 계정코드
    credit_account: Mapped[str | None] = mapped_column(String(20))  # 대변 계정코드
    amount_source: Mapped[str | None] = mapped_column(String(120))  # payload 금액 필드(콤마 우선순위)
    reverse_on_negative: Mapped[bool] = mapped_column(Boolean, default=False)  # clawback 차대반전
    skip: Mapped[bool] = mapped_column(Boolean, default=False)      # 무전표(정보성)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)     # 낮을수록 우선
    description: Mapped[str | None] = mapped_column(String(200))
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DealerSettlementCallback(Base):
    """검진권 정산 지급 회신 아웃박스 (ERP→HIS settlement.paid) — 전송보장·멱등·백오프.

    정산 전표 부킹 시 enqueue(event_id UNIQUE 멱등) → 송신 job이 HIS 콜백 EP로 POST·재시도.
    DB-4 계약(erp-settlement-paid-callback.contract.md) 송신측.
    """
    __tablename__ = "dealer_settlement_callbacks"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_settle_cb_event"),
        CheckConstraint("status IN ('QUEUED','SENT','FAILED','DLQ')", name="ck_settle_cb_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(80))       # erp:settle-paid:<settlementId>(멱등)
    settlement_id: Mapped[str | None] = mapped_column(String(64))
    distributor_id: Mapped[str | None] = mapped_column(String(64))
    amount_paid_krw: Mapped[float] = mapped_column(Numeric(14, 0))
    withholding_krw: Mapped[float | None] = mapped_column(Numeric(14, 0))
    fx_rate: Mapped[float | None] = mapped_column(Numeric(12, 4))
    erp_voucher_ref: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(8), default="QUEUED")  # QUEUED|SENT|FAILED|DLQ
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WithholdingRate(Base):
    """원천징수 요율 (시점 버전관리) — 세법 정기 개정 대응. 관리자 설정.

    검진권 딜러 정산 시 적용 요율을 시행일 구간(effective_from~to, NULL=현행)으로 관리.
    HIS가 withholdingTax를 동봉하면 그 값 우선, 없으면 시점 요율로 ERP가 재계산.
    해외 딜러는 보통 비대상(요율 미설정=무원천징수).
    """
    __tablename__ = "withholding_rates"
    __table_args__ = ({"schema": "finance"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    dealer_type: Mapped[str | None] = mapped_column(String(12))     # DOMESTIC|NULL=전체
    rate_pct: Mapped[float] = mapped_column(Numeric(6, 3))          # 예 3.300(%)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)         # NULL=현행
    withholding_account: Mapped[str] = mapped_column(String(20), default="2150")
    tax_invoice_by: Mapped[str | None] = mapped_column(String(20))  # 병원역발행|딜러발행(메모)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(200))
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class IntangibleAsset(Base):
    """무형자산(K-IFRS 1038) — 소프트웨어·개발비 등. 정액 상각, 차입원가 자본화 가능(1023)."""
    __tablename__ = "intangible_assets"
    __table_args__ = (
        CheckConstraint("status IN ('IN_USE','DISPOSED')", name="ck_intangible_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    asset_no: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    acquired_on: Mapped[date] = mapped_column(Date)
    cost: Mapped[float] = mapped_column(Numeric(16, 0))
    useful_life_years: Mapped[int] = mapped_column(Integer)
    accumulated_amort: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    capitalized_borrowing: Mapped[float] = mapped_column(Numeric(16, 0), default=0)  # 자본화 차입원가(1023)
    accumulated_impairment: Mapped[float] = mapped_column(Numeric(16, 0), default=0)  # 손상누계(1036)
    disposed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="IN_USE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AllowanceRate(Base):
    """대손충당금 설정(K-IFRS 1109) — 연령 버킷별 기대신용손실률. (kind,bucket) 고유.

    버킷: current/1-30/31-60/61-90/90+ (settlement_service.aging 일치). 결산 시 미수금 잔액×요율로
    충당액 산출 → 차액만 결산분개. 거시지표 반영 forward-looking ECL은 후속.
    """
    __tablename__ = "allowance_rates"
    __table_args__ = (
        UniqueConstraint("kind", "bucket", name="uq_allowance_rate"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(3), default="AR")     # AR(미수금)
    bucket: Mapped[str] = mapped_column(String(10))                # current|1-30|31-60|61-90|90+
    rate: Mapped[float] = mapped_column(Numeric(6, 4))             # 0.0000~1.0000
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RelatedParty(Base):
    """특수관계자(K-IFRS 1024) — 이사·주요경영진·관계기관 등. 거래는 RelatedPartyTxn."""
    __tablename__ = "related_parties"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    relation: Mapped[str] = mapped_column(String(40))         # 이사·주요경영진·관계기관·기타
    note: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RelatedPartyTxn(Base):
    """특수관계자 거래·잔액(K-IFRS 1024) — 기간(연)별 거래유형·금액·잔액."""
    __tablename__ = "related_party_txns"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    party_id: Mapped[int] = mapped_column(ForeignKey("finance.related_parties.id"), index=True)
    period: Mapped[str] = mapped_column(String(4))            # yyyy
    txn_type: Mapped[str] = mapped_column(String(40))         # 매출·매입·대여·차입·보수 등
    amount: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    balance: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    note: Mapped[str | None] = mapped_column(String(200))


class Contingency(Base):
    """우발부채·우발자산(K-IFRS 1037) — 현재의무 미충족/추정 곤란 항목."""
    __tablename__ = "contingencies"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(4))            # yyyy
    kind: Mapped[str] = mapped_column(String(10), default="LIABILITY")  # LIABILITY|ASSET
    description: Mapped[str] = mapped_column(String(300))
    estimate: Mapped[float | None] = mapped_column(Numeric(16, 0))      # 추정액(가능 시)
    likelihood: Mapped[str] = mapped_column(String(10), default="중간")  # 높음|중간|낮음
    status: Mapped[str] = mapped_column(String(16), default="OPEN")     # OPEN|PROVISIONED(11자)|RESOLVED
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SubsequentEvent(Base):
    """보고기간후사건(K-IFRS 1010) — 수정/비수정 사건."""
    __tablename__ = "subsequent_events"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(4))            # yyyy(보고기간)
    event_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(300))
    adjusting: Mapped[bool] = mapped_column(Boolean, default=False)     # 수정(T)/비수정(F)
    reflected: Mapped[bool] = mapped_column(Boolean, default=False)     # 수정사건 결산 반영 여부
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AccountingChange(Base):
    """회계정책·추정변경·오류(K-IFRS 1008) — 변경유형·처리방법(소급/전진)·영향."""
    __tablename__ = "accounting_changes"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(4))            # yyyy
    change_type: Mapped[str] = mapped_column(String(10))      # POLICY|ESTIMATE|ERROR
    standard: Mapped[str | None] = mapped_column(String(40))  # 관련 기준서
    description: Mapped[str] = mapped_column(String(400))
    treatment: Mapped[str] = mapped_column(String(14), default="PROSPECTIVE")  # RETROSPECTIVE|PROSPECTIVE
    effect_amount: Mapped[float | None] = mapped_column(Numeric(16, 0))         # 영향액(추정)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DisclosureNote(Base):
    """재무제표 주석(K-IFRS 1001) — 기간×주석키. 정량주석은 자동 산출, 정성주석은 수기 편집(payload JSON)."""
    __tablename__ = "disclosure_notes"
    __table_args__ = (
        UniqueConstraint("period", "note_key", name="uq_disclosure_note"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(7))            # yyyy 또는 yyyy-mm
    note_key: Mapped[str] = mapped_column(String(40))         # accounting_policy|ppe_movement|...
    title: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TaxRateSetting(Base):
    """법인세율 설정(K-IFRS 1012) — 이연법인세 측정용 유효세율. valid_from 시점관리.

    한국 법인세는 누진(9/19/21/24%)이나 본 기초 구현은 단일 유효세율로 단순화(한계 명시).
    """
    __tablename__ = "tax_rate_settings"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    rate: Mapped[float] = mapped_column(Numeric(6, 4))            # 유효 법인세율 0.0000~1.0000
    valid_from: Mapped[date] = mapped_column(Date, index=True)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SimScenario(Base):
    """재무 시뮬레이션 시나리오 — 사용자별 드라이버 가정 저장(서버 영속).

    drivers JSON = 프론트 Drivers(성장률·변동비율·고정비증감·세율·투자·차입·계절성).
    user_id 소유자 스코프 — 본인 시나리오만 조회/수정/삭제. (user_id,name) 고유.
    """
    __tablename__ = "sim_scenarios"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_sim_scenario_user_name"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(80))
    drivers: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CorporateAccount(Base):
    """법인 자금계좌 마스터 — 은행·계좌번호·통화별 법인계좌. 외화는 원화로 섞지 않고 통화별 분리.

    gl_account_code로 GL 계정 매핑(KRW=1110 현금·외화=1115 외화예금 등) → 입출금 전표가 이
    계좌의 GL로 부킹. is_default=통화별 기본 계좌(외화 voucher 라우팅 시 선택). 외화 잔액·환산은
    Stage 2(외화 라우팅·환산차손익)에서 연결.
    """
    __tablename__ = "corporate_accounts"
    __table_args__ = (
        UniqueConstraint("bank_name", "account_no", name="uq_corp_acct"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(String(60))                  # 별칭(예: 신한 USD 외화)
    bank_name: Mapped[str] = mapped_column(String(60))
    account_no: Mapped[str] = mapped_column(String(40))             # 계좌번호(내부관리·finance 권한)
    currency: Mapped[str] = mapped_column(String(3), default="KRW")  # KRW|USD|EUR|JPY...
    gl_account_code: Mapped[str] = mapped_column(String(20))        # 매핑 GL(1110·1115 외화예금 등)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)  # 통화별 기본 계좌
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    holder: Mapped[str | None] = mapped_column(String(60))          # 예금주
    swift: Mapped[str | None] = mapped_column(String(16))           # 외화 송금 SWIFT/BIC
    note: Mapped[str | None] = mapped_column(String(200))
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuditReview(Base):
    """감사·이상탐지 후보의 검토 상태 (ADR D-7) — finding_key로 재계산 후에도 상태 유지.

    finding_type=ABNORMAL → finding_key='voucher:<id>', DUPLICATE → 'dup:<sha1(전표id들)>'.
    Benford(분포 신호)는 항목 검토 대상이 아니라 미포함. 검토는 기록 행위 → reviewed_by 보존.
    """
    __tablename__ = "audit_reviews"
    __table_args__ = (
        UniqueConstraint("finding_type", "finding_key", name="uq_audit_review"),
        CheckConstraint("finding_type IN ('ABNORMAL','DUPLICATE')", name="ck_audit_finding_type"),
        CheckConstraint("status IN ('OPEN','REVIEWED')", name="ck_audit_review_status"),
        CheckConstraint("disposition IN ('NORMAL','ACTION') OR disposition IS NULL",
                        name="ck_audit_review_disposition"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    finding_type: Mapped[str] = mapped_column(String(10))   # ABNORMAL | DUPLICATE
    finding_key: Mapped[str] = mapped_column(String(80))    # voucher:<id> | dup:<sha1>
    status: Mapped[str] = mapped_column(String(8), default="REVIEWED")
    disposition: Mapped[str | None] = mapped_column(String(8))  # NORMAL(정상·오탐) | ACTION(조치필요)
    note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class Loan(Base):
    """차입금 (자금관리) — 원금·연이율·상환스케줄. 단기(2310)/장기(2510), 이자 5720. 월 이자 인식."""
    __tablename__ = "loans"
    __table_args__ = (
        CheckConstraint("kind IN ('SHORT','LONG')", name="ck_loan_kind"),
        CheckConstraint("status IN ('ACTIVE','REPAID')", name="ck_loan_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    lender: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(8), default="SHORT")     # SHORT(2310)|LONG(2510)
    principal: Mapped[float] = mapped_column(Numeric(16, 0))
    annual_rate: Mapped[float] = mapped_column(Numeric(6, 4))
    started_on: Mapped[date] = mapped_column(Date)
    term_months: Mapped[int] = mapped_column(Integer)
    repaid_principal: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE")
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccountBalanceCheckpoint(Base):
    """계정 잔액 체크포인트(성능 H-2) — 경계일(through)까지의 계정별 누적 차/대변합.

    balance_sheet가 개시부터 전 voucher_line을 재스캔하던 것을 `체크포인트 + 델타(through+1일~as_of)`로
    축소(대규모 성장 시 O(전체)→O(당기)). `_by_account(until=through)`와 동일 필터(POSTED·REVERSED)로
    적재 → 체크포인트+델타 == 전구간 스캔(자기검증·verify_balance_checkpoint). 배치가 마감월 경계마다 적재.
    """
    __tablename__ = "account_balance_checkpoints"
    __table_args__ = (
        UniqueConstraint("through", "account_code", name="uq_balchkpt"),
        Index("ix_balchkpt_through", "through"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    through: Mapped[date] = mapped_column(Date)            # 이 날짜(포함)까지 누적
    account_code: Mapped[str] = mapped_column(String(20))
    cum_debit: Mapped[float] = mapped_column(Numeric(20, 0))
    cum_credit: Mapped[float] = mapped_column(Numeric(20, 0))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PreauthOrder(Base):
    """사전승인 고가오더(관리회계·GL 미전기) — HIS preauth.order_approved 수신 기록.

    사전승인=약정(commitment)이라 GL 전기는 시기상조(실제 비용은 발주/입고 시 발생). 카테고리별 집계
    (category_summary 리포트)로 관리회계에만 반영. order_ref UNIQUE로 멱등(재수신 안전).
    """
    __tablename__ = "preauth_orders"
    __table_args__ = (
        UniqueConstraint("order_ref", name="uq_preauth_order"),
        Index("ix_preauth_cat_on", "category", "approved_on"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    order_ref: Mapped[str] = mapped_column(String(64))       # HIS 오더 식별(멱등키)
    category: Mapped[str] = mapped_column(String(40), default="UNCLASSIFIED")  # 고가오더 분류
    amount: Mapped[float] = mapped_column(Numeric(16, 0))    # 승인 예상금액(원)
    dept_code: Mapped[str | None] = mapped_column(String(10))
    approver: Mapped[str | None] = mapped_column(String(80))
    approved_on: Mapped[date] = mapped_column(Date, index=True)
    note: Mapped[str | None] = mapped_column(String(300))
    # 승인 채널 — STANDARD(내부결재)·PORTAL(포털승인)·EMERGENCY_BYPASS(응급 POST_FACTO 우회)
    channel: Mapped[str] = mapped_column(String(20), default="STANDARD")
    # 임상 오더 유형(규제·감사 축) — 마약류·수혈·고가검사 등(카테고리=회계 축과 별개)
    order_type: Mapped[str | None] = mapped_column(String(40))
    # 사후승인 완료 여부 — 응급우회는 False(사후승인 미결)로 인지·집계, 사후승인 시 True
    post_approved: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 지출·경비 관리 (내부운영 갭 감사 Tier1·마이그 0210) ─────────────────────
class ExpenseClaim(Base):
    """직원 경비 청구 — 기안(직원)→상신(결재선)→승인(비용/미지급 전기)→지급.

    승인 시 DR 비용계정(라인별) / CR 미지급금(개인분 2110·법인카드분 2112). 지급은 개인분만
    (카드분은 카드사 월정산 settle_card_month). 영수증 첨부 = 범용 Attachment(entity_type="expense").
    """
    __tablename__ = "expense_claims"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','PENDING','APPROVED','REJECTED','PAID','CANCELED')",
                        name="ck_expense_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(BigInteger, index=True)   # hr.employees(크로스모듈 키)
    department_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(300))
    claim_on: Mapped[date] = mapped_column(Date, index=True)           # 청구(귀속) 기준일
    total_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    status: Mapped[str] = mapped_column(String(10), default="DRAFT", index=True)
    voucher_ref: Mapped[str | None] = mapped_column(String(64))        # 승인(비용) 전표
    payment_voucher_ref: Mapped[str | None] = mapped_column(String(64))  # 지급 전표(개인분)
    paid_on: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[int] = mapped_column(BigInteger)                # 기안자 user id(SoD)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExpenseLine(Base):
    """경비 청구 라인 — 사용일·비용계정·사용처·금액(+법인카드 사용내역 연결)."""
    __tablename__ = "expense_lines"
    __table_args__ = ({"schema": "finance"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("finance.expense_claims.id"), index=True)
    expense_on: Mapped[date] = mapped_column(Date)
    account_code: Mapped[str] = mapped_column(String(20))              # 비용(EXPENSE) 계정
    description: Mapped[str] = mapped_column(String(200))
    merchant: Mapped[str | None] = mapped_column(String(120))          # 사용처
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    card_tx_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # 법인카드 사용내역


class CorporateCard(Base):
    """법인카드 마스터 — 카드번호는 마지막 4자리만 저장(PII·PCI 최소화·전체 번호 저장 금지)."""
    __tablename__ = "corporate_cards"
    __table_args__ = ({"schema": "finance"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(String(60), unique=True)        # 예: 법인카드(원무1)
    card_last4: Mapped[str] = mapped_column(String(4))
    holder_employee_id: Mapped[int | None] = mapped_column(BigInteger)  # 소지자(공용이면 NULL)
    department_id: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(default=True)
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CardTransaction(Base):
    """법인카드 사용내역 — 카드사 승인번호(tx_id) 멱등 적재 → 경비청구 연결 → 월정산 대사.

    생애주기: UNMATCHED(적재)→CLAIMED(경비청구 라인 연결)→SETTLED(월정산 지급·2112 해소).
    """
    __tablename__ = "card_transactions"
    __table_args__ = (
        UniqueConstraint("tx_id", name="uq_card_tx"),
        CheckConstraint("status IN ('UNMATCHED','CLAIMED','SETTLED')", name="ck_card_tx_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("finance.corporate_cards.id"), index=True)
    tx_id: Mapped[str] = mapped_column(String(60))                     # 카드사 승인번호(멱등)
    tx_on: Mapped[date] = mapped_column(Date, index=True)
    merchant: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    status: Mapped[str] = mapped_column(String(10), default="UNMATCHED", index=True)
    claim_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # 연결 경비청구
    settled_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 부대사업 수익(의료외수익) — 주차·임대·비보험검진 등 비진료 수익 (마이그 0215) ──
class AncillaryRevenueItem(Base):
    """의료외수익 항목 마스터 — 기관이 실제 운영하는 부대사업만 활성화(범용 설정형).

    의료용역은 부가세 면세(부가세법 §26①5)이나 부대사업(주차·임대 등)은 원칙 과세 → taxable 시
    수납액을 순액(4130)+부가세예수금(2135)으로 분리. 병원회계기준상 의료외수익 별도 분류(4130).
    """
    __tablename__ = "ancillary_revenue_items"
    __table_args__ = (
        CheckConstraint(
            "category IN ('PARKING','RENTAL','CAFETERIA','CHECKUP','FUNERAL','OTHER')",
            name="ck_ancrev_cat"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))                 # 예: 외래주차·장례식장 임대
    category: Mapped[str] = mapped_column(String(12), default="OTHER", index=True)
    account_code: Mapped[str] = mapped_column(String(20), default="4130")  # 의료외수익
    taxable: Mapped[bool] = mapped_column(Boolean, default=True)  # 부가세 과세(부대사업 원칙 과세)
    default_price: Mapped[float | None] = mapped_column(Numeric(14, 0))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AncillaryRevenue(Base):
    """의료외수익 발생·수납 — DR 현금(1110)/카드미수(1132) / CR 4130 순액 (+ 과세 시 2135 부가세예수금)."""
    __tablename__ = "ancillary_revenues"
    __table_args__ = (
        # 멱등키(F5) — 클라이언트 재전송 시 중복 수익/전표 방지. NULL 다중 허용(부분 유니크)
        Index("ux_ancillary_client_ref", "client_ref", unique=True,
              sqlite_where=text("client_ref IS NOT NULL"),
              postgresql_where=text("client_ref IS NOT NULL")),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("finance.ancillary_revenue_items.id"),
                                         index=True)
    client_ref: Mapped[str | None] = mapped_column(String(64))   # 멱등키(F5·재전송 디둡)
    amount: Mapped[float] = mapped_column(Numeric(14, 0))         # 수납 총액(부가세 포함)
    net_amount: Mapped[float] = mapped_column(Numeric(14, 0))     # 순매출(4130)
    vat_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)  # 매출세액(2135)
    method: Mapped[str] = mapped_column(String(10), default="CASH")   # CASH|CARD|TRANSFER
    qty: Mapped[float] = mapped_column(Numeric(12, 2), default=1)
    occurred_on: Mapped[date] = mapped_column(Date, index=True)
    note: Mapped[str | None] = mapped_column(String(200))
    voucher_ref: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 고정자산 부서이동·실사 (마이그 0218) ─────────────────────────────
class AssetTransfer(Base):
    """고정자산 부서간 이동 이력 — department_id UPDATE에 감사추적 없던 갭 보완."""
    __tablename__ = "asset_transfers"
    __table_args__ = ({"schema": "finance"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("finance.fixed_assets.id"), index=True)
    from_dept: Mapped[int | None] = mapped_column(BigInteger)
    to_dept: Mapped[int | None] = mapped_column(BigInteger)
    transferred_on: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str | None] = mapped_column(String(300))
    actor: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssetInventory(Base):
    """고정자산 실사(재물조사) 세션 — 자산 실물 대사(위치/부서·소재 확인). OPEN→CLOSED."""
    __tablename__ = "asset_inventories"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_assetinv_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))            # 예: 2026 상반기 재물조사
    conducted_on: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(8), default="OPEN", index=True)
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssetInventoryLine(Base):
    """실사 라인 — 개시 시 IN_USE 자산 스냅샷(기대 부서). 확인 결과 FOUND/NOT_FOUND/RELOCATED."""
    __tablename__ = "asset_inventory_lines"
    __table_args__ = (
        CheckConstraint("found_status IN ('NOT_CHECKED','FOUND','NOT_FOUND','RELOCATED')",
                        name="ck_assetinvline_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    inventory_id: Mapped[int] = mapped_column(ForeignKey("finance.asset_inventories.id"),
                                              index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("finance.fixed_assets.id"), index=True)
    expected_dept: Mapped[int | None] = mapped_column(BigInteger)   # 개시 스냅샷(당시 부서)
    found_status: Mapped[str] = mapped_column(String(12), default="NOT_CHECKED")
    actual_dept: Mapped[int | None] = mapped_column(BigInteger)     # 실제 확인 부서(이전 시)
    note: Mapped[str | None] = mapped_column(String(200))
    checked_by: Mapped[int | None] = mapped_column(BigInteger)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DisbursementBatch(Base):
    """AP 지급결의 배치 (자금 일괄 지급) — 미지급금 다건을 배치로 결의→승인(SoD)→실행(전표·이체파일).

    DRAFT(결의 작성)→APPROVED(승인·작성자≠승인자)→EXECUTED(지급 전기 DR2110/CR1110·이체파일)/CANCELED.
    실행 시 각 라인이 SettlementService.pay 재사용(멱등 source_ref). 미지급금은 활성 배치 1개에만 포함.
    """
    __tablename__ = "disbursement_batches"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','APPROVED','EXECUTED','CANCELED')",
                        name="ck_disb_status"),
        {"schema": "finance"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    batch_no: Mapped[str] = mapped_column(String(24), unique=True)
    status: Mapped[str] = mapped_column(String(12), default="DRAFT", index=True)
    scheduled_on: Mapped[date] = mapped_column(Date)         # 지급 예정일
    total_amount: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    executed_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DisbursementLine(Base):
    """지급결의 라인 — 배치 ↔ 미지급금. amount=이번 지급액(부분지급 가능). bank_account=이체계좌(선택)."""
    __tablename__ = "disbursement_lines"
    __table_args__ = ({"schema": "finance"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("finance.disbursement_batches.id"), index=True)
    payable_id: Mapped[int] = mapped_column(ForeignKey("finance.payables.id"), index=True)
    party: Mapped[str] = mapped_column(String(120))
    party_ref: Mapped[str | None] = mapped_column(String(40))
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    due_on: Mapped[date | None] = mapped_column(Date)
    bank_account: Mapped[str | None] = mapped_column(String(60))   # 이체계좌(선택·수기/추후 마스터)
    status: Mapped[str] = mapped_column(String(10), default="PENDING")  # PENDING|PAID
