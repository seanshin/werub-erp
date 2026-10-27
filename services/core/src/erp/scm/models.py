"""scm 스키마 — design §3.4/§3.5. 마약류 원장은 append-only (DB REVOKE는 0004)."""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, LargeBinary,
    Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp.db import Base, BigIntPK


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = {"schema": "scm"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    # 해외 검진권 딜러는 국내 사업자번호가 없음 → nullable(마이그 0070). 국내는 추후 taxId 수신.
    business_no: Mapped[str | None] = mapped_column(String(12), unique=True)
    contact: Mapped[str | None] = mapped_column(String(120))
    external_vendor_code: Mapped[str | None] = mapped_column(String(40))  # HIS/공통 거래처 매핑

    # 검진권 딜러 마스터 (HIS vendor.synced/changed 수신 — B-G1-A, 마이그 0070)
    vendor_type: Mapped[str | None] = mapped_column(String(24))     # VOUCHER_DEALER 등(null=일반 공급사)
    dealer_type: Mapped[str | None] = mapped_column(String(12))     # DOMESTIC(원천징수)|OVERSEAS(외화)
    tier: Mapped[str | None] = mapped_column(String(12))            # MASTER(총판)|AGENT(대리점)
    commission_rate: Mapped[float | None] = mapped_column(Numeric(5, 2))  # 수수료율(%)
    country: Mapped[str | None] = mapped_column(String(2))          # ISO-2
    currency: Mapped[str | None] = mapped_column(String(3))         # ISO-3(정산통화)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # status=TERMINATED → False
    status: Mapped[str] = mapped_column(String(12), default="APPROVED")  # PENDING|APPROVED|SUSPENDED|TERMINATED
    eval_score: Mapped[float | None] = mapped_column(Numeric(4, 1))  # 최근 평가 총점(0~100)
    # 컴플라이언스·계층 (HIS payload 보존 — 마이그 0071)
    license_number: Mapped[str | None] = mapped_column(String(40))  # 국내: 외국인환자 유치업 등록번호(법정)
    contract_ref: Mapped[str | None] = mapped_column(String(60))    # 해외: 병원 제휴계약 참조
    parent_external_code: Mapped[str | None] = mapped_column(String(40))  # 상위 딜러 dealerId(계층)
    # 세무·송금 마스터 (HIS B2-a — 정산 AP 송금). 민감정보(사업자번호·계좌·SWIFT)는
    # AES-GCM 컬럼암호화(HR 보수 프로필 표준, design §6.4·마이그 0078). 은행명만 평문.
    tax_id_enc: Mapped[bytes | None] = mapped_column(LargeBinary)        # 사업자번호/TIN(암호화)
    bank_account_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # 계좌번호(암호화)
    bank_swift_enc: Mapped[bytes | None] = mapped_column(LargeBinary)    # SWIFT/BIC(암호화)
    bank_name: Mapped[str | None] = mapped_column(String(60))            # 은행명(비민감·평문)


class Item(Base):
    """품목 마스터 (FR-11) — 마약류 플래그·안전재고."""
    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint("category IN ('DRUG','SUPPLY','EQUIPMENT')", name="ck_item_cat"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    barcode: Mapped[str | None] = mapped_column(String(40), unique=True)  # 워크스테이션 스캔(자체발번/GTIN)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(12))
    unit: Mapped[str] = mapped_column(String(10), default="EA")
    is_narcotic: Mapped[bool] = mapped_column(Boolean, default=False)  # FR-20
    safety_stock_qty: Mapped[float] = mapped_column(Numeric(12, 2), default=0)  # FR-13 재주문점
    par_level: Mapped[float | None] = mapped_column(Numeric(12, 2))  # 보충 목표(order-up-to)·자동발주 제안
    nrv_unit: Mapped[float | None] = mapped_column(Numeric(14, 2))      # 순실현가능가치 단가(저가법 K-IFRS 1002)
    cost_method: Mapped[str] = mapped_column(String(8), default="FIFO")  # FIFO|WAVG 원가흐름


class Lot(Base):
    """로트 — 유효기간 필수, 잔량 음수 방어 (FR-11/46)."""
    __tablename__ = "lots"
    __table_args__ = (
        UniqueConstraint("item_id", "lot_no", name="uq_lot"),
        CheckConstraint("remaining_qty >= 0", name="ck_lot_nonneg"),  # 음수 방어 (design v0.3)
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"), index=True)
    lot_no: Mapped[str] = mapped_column(String(40))
    expiry_on: Mapped[date] = mapped_column(Date)            # 필수 입력 (FR-11)
    received_on: Mapped[date] = mapped_column(Date)
    received_qty: Mapped[float] = mapped_column(Numeric(12, 2))
    remaining_qty: Mapped[float] = mapped_column(Numeric(12, 2))
    unit_cost: Mapped[float | None] = mapped_column(Numeric(14, 2))   # 매입단가(재고평가)
    supplier_id: Mapped[int | None] = mapped_column(BigInteger)


class StockMovement(Base):
    """입출고 (FR-12) — 출고는 FIFO 엔진이 lot 결정."""
    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint("movement_type IN ('IN','OUT','DISPOSE','ADJUST','RETURN')",
                        name="ck_mv_type"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("scm.lots.id"))
    movement_type: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Numeric(12, 2))
    moved_on: Mapped[date] = mapped_column(Date)
    actor: Mapped[int] = mapped_column(BigInteger)
    override_reason: Mapped[str | None] = mapped_column(String(200))  # 임박 lot 강제 출고 기록
    cost: Mapped[float | None] = mapped_column(Numeric(16, 2))  # 출고원가(COGS, cost_method 적용 K-IFRS 1002)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NarcoticsLedger(Base):
    """마약류 수불부 (FR-20) — append-only·해시 체인·정정 레코드 (design §3.4)."""
    __tablename__ = "narcotics_ledger"
    __table_args__ = (
        UniqueConstraint("item_id", "seq_no", name="uq_narc_seq"),
        CheckConstraint("entry_type IN ('IN','OUT','DISPOSE','ADJUST','CORRECTION')",
                        name="ck_narc_type"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    seq_no: Mapped[int] = mapped_column(BigInteger)          # 품목별 연속 순번
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"), index=True)
    lot_id: Mapped[int | None] = mapped_column(BigInteger)
    entry_type: Mapped[str] = mapped_column(String(10))
    qty: Mapped[float] = mapped_column(Numeric(12, 2))       # 부호 포함 변화량
    balance_after: Mapped[float] = mapped_column(Numeric(12, 2))  # 시스템 산출 — 입력 금지
    handler_id: Mapped[int] = mapped_column(BigInteger)      # 취급자
    entry_on: Mapped[date | None] = mapped_column(Date)     # 거래일(월 수불일지 귀속 — FR-21).
    # nullable: append-only(UPDATE revoke)로 legacy 행 백필 불가 → monthly_ledger가 created_at 폴백.
    corrects_id: Mapped[int | None] = mapped_column(BigInteger)  # CORRECTION → 원본만 참조
    correction_reason: Mapped[str | None] = mapped_column(Text)
    row_hash: Mapped[str] = mapped_column(String(64))        # SHA-256(prev_hash ‖ row)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PurchaseOrder(Base):
    """발주 (FR-14) — 요청→승인→입고."""
    __tablename__ = "purchase_orders"
    __table_args__ = (
        CheckConstraint("status IN ('REQUESTED','APPROVED','PARTIAL','RECEIVED','REJECTED')",
                        name="ck_po_status"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("scm.suppliers.id"))
    status: Mapped[str] = mapped_column(String(12), default="REQUESTED")
    requested_by: Mapped[int] = mapped_column(BigInteger)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    department_id: Mapped[int | None] = mapped_column(BigInteger)  # 예산 귀속 부서(약정 연계)
    account_code: Mapped[str] = mapped_column(String(20), default="1150")  # 예산 계정(기본 저장품)
    receipt_count: Mapped[int] = mapped_column(Integer, default=0)  # 분할입고 채번(미지급 멱등키)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PoLine(Base):
    __tablename__ = "po_lines"
    __table_args__ = {"schema": "scm"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("scm.purchase_orders.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"))
    qty: Mapped[float] = mapped_column(Numeric(12, 2))
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2))
    received_qty: Mapped[float] = mapped_column(Numeric(12, 2), default=0)  # 누적 입고(3-way 대사)


class StockTake(Base):
    """재고 실사 — 실재고 입력→장부 대비 차이→로트 ADJUST·감모손실/조정이익 전표. DRAFT→APPLIED."""
    __tablename__ = "stock_takes"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','APPLIED')", name="ck_stocktake_status"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"), index=True)
    taken_on: Mapped[date] = mapped_column(Date)
    book_qty: Mapped[float] = mapped_column(Numeric(12, 2))
    physical_qty: Mapped[float] = mapped_column(Numeric(12, 2))
    diff_qty: Mapped[float] = mapped_column(Numeric(12, 2))
    unit_cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    diff_value: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    status: Mapped[str] = mapped_column(String(8), default="DRAFT")
    voucher_ref: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutsourcingContract(Base):
    """구매·위수탁 계약 (CLM) — 급식·세탁 등 위탁 + 일반 구매·용역·임대 계약 기간·금액 추적.

    contract_type으로 위수탁/구매/용역/임대 구분. billing_type=MONTHLY는 월 위탁료 accrue로 미지급금
    발생(멱등)→지급·세금계산서 대사 연결, ONE_TIME(일회성 구매/용역)은 월 accrue 제외(생애주기·이력은 동일).
    """
    __tablename__ = "outsourcing_contracts"
    __table_args__ = (
        CheckConstraint("category IN ('MEAL','LAUNDRY','WASTE','SECURITY',"
                        "'CLEANING','FACILITY','IT','INSURANCE','OTHER')", name="ck_oc_category"),
        CheckConstraint(
            "contract_type IN ('OUTSOURCING','PURCHASE','SERVICE','LEASE','INSURANCE','OTHER')",
            name="ck_oc_type"),
        CheckConstraint("billing_type IN ('MONTHLY','ONE_TIME')", name="ck_oc_billing"),
        CheckConstraint("status IN ('DRAFT','PENDING_APPROVAL','APPROVED','ACTIVE',"
                        "'EXPIRING','EXPIRED','TERMINATED','REJECTED')", name="ck_oc_status"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    vendor_name: Mapped[str] = mapped_column(String(120))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("scm.suppliers.id"))
    category: Mapped[str] = mapped_column(String(12))
    contract_type: Mapped[str] = mapped_column(String(16), default="OUTSOURCING")
    billing_type: Mapped[str] = mapped_column(String(10), default="MONTHLY")
    title: Mapped[str] = mapped_column(String(200))
    monthly_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)  # MONTHLY=월액·ONE_TIME=총액
    start_on: Mapped[date] = mapped_column(Date)
    end_on: Mapped[date] = mapped_column(Date, index=True)
    effective_on: Mapped[date | None] = mapped_column(Date)   # 발효일(기본=start_on)
    auto_renew: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # 생애주기 단계
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminate_reason: Mapped[str | None] = mapped_column(String(200))
    terminated_on: Mapped[date | None] = mapped_column(Date)
    # 보험계약 전용(마이그 0220) — contract_type=INSURANCE일 때. 배상책임/화재/의료기기 보험 만기관리.
    policy_no: Mapped[str | None] = mapped_column(String(60))          # 증권번호
    coverage_limit: Mapped[float | None] = mapped_column(Numeric(16, 0))  # 보장한도
    premium_cycle: Mapped[str | None] = mapped_column(String(10))     # 납입주기 ANNUAL|SEMIANNUAL|QUARTERLY|MONTHLY
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 전자서명 미러(e-sign #6) — HIS sign 결과 수신(sign.completed). HIS=SoT, ERP는 미러.
    sign_envelope_id: Mapped[str | None] = mapped_column(String(64))
    sign_status: Mapped[str | None] = mapped_column(String(16))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sign_cert_serial: Mapped[str | None] = mapped_column(String(80))
    sign_assurance: Mapped[str | None] = mapped_column(String(20))
    sign_doc_ref: Mapped[str | None] = mapped_column(String(300))
    sign_doc_attachment_id: Mapped[int | None] = mapped_column(BigInteger)  # 회수한 완료본(Attachment)


class VendorEvaluation(Base):
    """공급사 평가 스코어카드 (구매 거버넌스) — 기간별 납기·품질·단가 점수. (supplier, period) 고유."""
    __tablename__ = "vendor_evaluations"
    __table_args__ = (
        UniqueConstraint("supplier_id", "period", name="uq_vendor_eval"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("scm.suppliers.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))             # yyyy-mm
    delivery_score: Mapped[float] = mapped_column(Numeric(4, 1))
    quality_score: Mapped[float] = mapped_column(Numeric(4, 1))
    price_score: Mapped[float] = mapped_column(Numeric(4, 1))
    total: Mapped[float] = mapped_column(Numeric(4, 1))
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DrugRecall(Base):
    """의약품 회수 (식약처 회수명령 대응) — 영향 로트 추적·격리. lot_no null=전 로트."""
    __tablename__ = "drug_recalls"
    __table_args__ = (
        CheckConstraint("severity IN ('VOLUNTARY','CLASS1','CLASS2','CLASS3')",
                        name="ck_recall_severity"),
        CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_recall_status"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"), index=True)
    lot_no: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(300))
    severity: Mapped[str] = mapped_column(String(10), default="VOLUNTARY")
    recall_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="OPEN")
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MedicalWasteLog(Base):
    """의료폐기물 배출대장 (폐기물관리법) — 종류·중량·보관·인계(올바로 인계서)."""
    __tablename__ = "medical_waste_logs"
    __table_args__ = (
        CheckConstraint("status IN ('STORED','HANDED_OVER')", name="ck_waste_status"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    waste_type: Mapped[str] = mapped_column(String(20))     # 격리·위해·일반·손상성·병리계
    weight_kg: Mapped[float] = mapped_column(Numeric(10, 2))
    stored_on: Mapped[date] = mapped_column(Date, index=True)
    handed_over_on: Mapped[date | None] = mapped_column(Date)
    handler: Mapped[str | None] = mapped_column(String(120))
    manifest_no: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(16), default="STORED")   # STORED|HANDED_OVER(11자)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ContractAmendment(계약 변경 이력)은 common.models로 승격 — 위수탁·의료 계약 공유(0139).


class Rfq(Base):
    """견적요청(RFQ) — 소싱 상류. 다수 벤더에 견적 요청→비교→낙찰→단가계약(price_agreements)."""
    __tablename__ = "rfqs"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','OPEN','CLOSED','AWARDED','CANCELED')",
                        name="ck_rfq_status"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(12))
    due_on: Mapped[date | None] = mapped_column(Date)        # 견적 마감일
    status: Mapped[str] = mapped_column(String(10), default="DRAFT")
    awarded_quote_id: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RfqLine(Base):
    __tablename__ = "rfq_lines"
    __table_args__ = {"schema": "scm"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    rfq_id: Mapped[int] = mapped_column(ForeignKey("scm.rfqs.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"))
    qty: Mapped[float] = mapped_column(Numeric(12, 2))
    spec: Mapped[str | None] = mapped_column(String(200))


class Quote(Base):
    """벤더 견적 — RFQ에 대한 공급사 응찰. 낙찰 시 단가계약 생성."""
    __tablename__ = "quotes"
    __table_args__ = (
        CheckConstraint("status IN ('SUBMITTED','AWARDED','REJECTED')", name="ck_quote_status"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    rfq_id: Mapped[int] = mapped_column(ForeignKey("scm.rfqs.id"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("scm.suppliers.id"))
    total: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    status: Mapped[str] = mapped_column(String(10), default="SUBMITTED")
    submitted_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuoteLine(Base):
    __tablename__ = "quote_lines"
    __table_args__ = {"schema": "scm"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("scm.quotes.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"))
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2))
    lead_time_days: Mapped[int | None] = mapped_column(Integer)


class PriceAgreement(Base):
    """단가계약 — 낙찰 견적 기반 품목별 계약단가(유효기간). PO가 계약단가 자동 적용."""
    __tablename__ = "price_agreements"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','EXPIRED','TERMINATED')", name="ck_pa_status"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("scm.suppliers.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    rfq_id: Mapped[int | None] = mapped_column(BigInteger)   # 출처 RFQ(있으면)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE")
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PriceAgreementLine(Base):
    __tablename__ = "price_agreement_lines"
    __table_args__ = (
        UniqueConstraint("agreement_id", "item_id", name="uq_paline_item"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    agreement_id: Mapped[int] = mapped_column(ForeignKey("scm.price_agreements.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"))
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2))


# ── 입고 검수·반품(RMA) — 불량/파손/유효기간/오배송 반품 + credit note (마이그 0214) ──
class PurchaseReturn(Base):
    """입고 후 공급사 반품 — 로트 잔량 차감(RETURN 이동)·미지급 credit note·재입고(교체) 재개.

    사유 = 불량(DEFECT)·파손(DAMAGE)·유효기간임박/도과(EXPIRY)·오배송(WRONG_ITEM)·기타. 반품 수량은
    로트 잔량 이내(이미 소비/폐기분 반품 불가). credit note = DR 미지급금(2110)/CR 재고자산(1150).
    """
    __tablename__ = "purchase_returns"
    __table_args__ = (
        CheckConstraint("reason IN ('DEFECT','DAMAGE','EXPIRY','WRONG_ITEM','OTHER')",
                        name="ck_pret_reason"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("scm.purchase_orders.id"), index=True)
    supplier_id: Mapped[int | None] = mapped_column(BigInteger)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("scm.lots.id"), index=True)
    qty: Mapped[float] = mapped_column(Numeric(12, 2))
    unit_cost: Mapped[float] = mapped_column(Numeric(14, 2))
    amount: Mapped[float] = mapped_column(Numeric(16, 0))
    reason: Mapped[str] = mapped_column(String(12), default="DEFECT")
    note: Mapped[str | None] = mapped_column(String(300))
    returned_on: Mapped[date] = mapped_column(Date, index=True)
    credit_voucher_ref: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 구매요청(PR)·내부 자재청구 — 요청→승인→실행 앞단 (마이그 0217) ─────────
class PurchaseRequisition(Base):
    """구매요청(PR) — 부서 수요 기안→승인→PO 전환. PO가 앞단 없이 시작하던 갭 보완.

    상태: PENDING→APPROVED→CONVERTED(PO 전환)·REJECTED·CANCELED. 승인 후에만 발주 전환(직무분리).
    """
    __tablename__ = "purchase_requisitions"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','CONVERTED','CANCELED')",
                        name="ck_preq_status"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    req_no: Mapped[str] = mapped_column(String(24), unique=True)     # 구매요청-YYYY-NNNN
    department_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    requester: Mapped[int] = mapped_column(BigInteger)              # 기안자 user id(SoD)
    reason: Mapped[str | None] = mapped_column(String(300))
    needed_by: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    po_id: Mapped[int | None] = mapped_column(BigInteger)           # 전환된 발주
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RequisitionLine(Base):
    """구매요청 라인 — 품목·수량·예상단가(참고)."""
    __tablename__ = "requisition_lines"
    __table_args__ = ({"schema": "scm"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    req_id: Mapped[int] = mapped_column(ForeignKey("scm.purchase_requisitions.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"))
    qty: Mapped[float] = mapped_column(Numeric(12, 2))
    est_price: Mapped[float | None] = mapped_column(Numeric(14, 2))  # 예상단가(참고)
    note: Mapped[str | None] = mapped_column(String(200))


class MaterialRequest(Base):
    """내부 자재청구(불출요청) — 병동/부서가 중앙창고 불출 요청→승인→출고. issue 무승인 즉시OUT 갭 보완.

    상태: REQUESTED→APPROVED→ISSUED·REJECTED·CANCELED. 승인 후에만 재고 출고(FIFO·직무분리).
    """
    __tablename__ = "material_requests"
    __table_args__ = (
        CheckConstraint("status IN ('REQUESTED','APPROVED','ISSUED','REJECTED','CANCELED')",
                        name="ck_mreq_status"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    req_no: Mapped[str] = mapped_column(String(24), unique=True)     # 자재청구-YYYY-NNNN
    department_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    requester: Mapped[int] = mapped_column(BigInteger)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"), index=True)
    qty: Mapped[float] = mapped_column(Numeric(12, 2))
    reason: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(12), default="REQUESTED", index=True)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    issued_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 위탁재고(Consignment) — 소유권 미이전 보관·사용 시점 매입 (마이그 0219) ──
class ConsignmentStock(Base):
    """위탁재고 — 고가 임플란트·스텐트 등 공급사 소유 상태로 병원 보관. 사용 시점에 매입(소유권 이전).

    적재(place)는 회계 없음(미소유). 사용(consume) 시 DR 재료비(5410)/CR 미지급금(2110)로 매입 인식.
    미사용 반품(return)도 회계 없음. 일반 Lot(소유재고)과 분리.
    """
    __tablename__ = "consignment_stock"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','DEPLETED','RETURNED')", name="ck_consign_status"),
        CheckConstraint("qty_on_hand >= 0", name="ck_consign_nonneg"),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(BigInteger, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("scm.items.id"), index=True)
    lot_no: Mapped[str] = mapped_column(String(40))
    expiry_on: Mapped[date | None] = mapped_column(Date)
    placed_qty: Mapped[float] = mapped_column(Numeric(12, 2))
    qty_on_hand: Mapped[float] = mapped_column(Numeric(12, 2))
    unit_cost: Mapped[float] = mapped_column(Numeric(14, 2))     # 사용 시 매입단가
    placed_on: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE", index=True)
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConsignmentConsumption(Base):
    """위탁재고 사용(매입 인식) — 수량·금액·미지급 전표 추적. chart_no로 시술 환자 연계(청구는 별도)."""
    __tablename__ = "consignment_consumptions"
    __table_args__ = (
        # 멱등키(F5) — 재전송 시 중복 매입인식(DR5410/CR2110)·미지급 방지. NULL 다중 허용
        Index("ux_consign_consume_client_ref", "client_ref", unique=True,
              sqlite_where=text("client_ref IS NOT NULL"),
              postgresql_where=text("client_ref IS NOT NULL")),
        {"schema": "scm"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    consignment_id: Mapped[int] = mapped_column(ForeignKey("scm.consignment_stock.id"), index=True)
    client_ref: Mapped[str | None] = mapped_column(String(64))   # 멱등키(F5·재전송 디둡)
    qty: Mapped[float] = mapped_column(Numeric(12, 2))
    unit_cost: Mapped[float] = mapped_column(Numeric(14, 2))
    amount: Mapped[float] = mapped_column(Numeric(16, 0))
    consumed_on: Mapped[date] = mapped_column(Date, index=True)
    chart_no: Mapped[str | None] = mapped_column(String(20), index=True)   # 시술 환자(청구 연계 참고)
    note: Mapped[str | None] = mapped_column(String(200))
    payable_ref: Mapped[str | None] = mapped_column(String(64))
    actor: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
