"""regulatory 스키마 모델 — design §3.2."""
import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON, BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Integer,
    Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp.db import Base, BigIntPK


class FeeMaster(Base):
    """수가/약제/치료재료 temporal 마스터 (FR-34/35).

    경계 규칙(설계 확정): valid_from inclusive(>=), valid_to exclusive(<), NULL=현행.
    기간 겹침 방어 EXCLUDE(GiST)는 마이그레이션 0001에서 추가 (PG 전용 — G4).
    """
    __tablename__ = "fee_master"
    __table_args__ = (
        UniqueConstraint("code", "code_type", "valid_from", name="uq_fee_version"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_fee_range"),
        CheckConstraint("code_type IN ('TARIFF','DRUG','MATERIAL','NONCOVERED')", name="ck_fee_type"),
        {"schema": "regulatory"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), index=True)
    code_type: Mapped[str] = mapped_column(String(16))
    benefit_category: Mapped[str | None] = mapped_column(String(16))  # 진료비 항목분류(0052)
    name: Mapped[str] = mapped_column(Text)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2))
    price_cap: Mapped[float | None] = mapped_column(Numeric(12, 2))
    criteria_text: Mapped[str | None] = mapped_column(Text)
    benefit_type: Mapped[str | None] = mapped_column(String(8))    # 급여 | 비급여 (치료재료·별도보상)
    reward_rate: Mapped[float | None] = mapped_column(Numeric(4, 2))  # 보상률(치료재료 별도보상 0~1)
    sex: Mapped[str | None] = mapped_column(String(1))          # 성별 적용제한 'M'|'F'(사전심사 G5)
    min_age: Mapped[int | None] = mapped_column(Integer)        # 연령 적용제한 하한
    max_age: Mapped[int | None] = mapped_column(Integer)        # 연령 적용제한 상한
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(8), default="API")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notice(Base):
    """공시 — 불변 erp_ref_id (FR-51, AI 동기화 키)."""
    __tablename__ = "notices"
    __table_args__ = (
        CheckConstraint(
            "notice_type IN ('TARIFF','DRUG_MATERIAL','REVIEW_CRITERIA','MEDICAL_LAW',"
            "'ADMIN','NARCOTIC','LABOR','INTERNAL')", name="ck_notice_type"),
        {"schema": "regulatory"},
    )

    erp_ref_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    notice_type: Mapped[str] = mapped_column(String(16))
    current_rev: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NoticeRevision(Base):
    __tablename__ = "notice_revisions"
    __table_args__ = (
        UniqueConstraint("erp_ref_id", "revision_no", name="uq_notice_rev"),
        CheckConstraint("status IN ('SCHEDULED','ACTIVE','REPEALED')", name="ck_notice_status"),
        {"schema": "regulatory"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    erp_ref_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regulatory.notices.erp_ref_id"))
    revision_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)          # 정규화 텍스트, 1MB 검증은 서비스
    content_hash: Mapped[str] = mapped_column(String(64))  # 정규화 7항 SHA-256
    effective_from: Mapped[date] = mapped_column(Date)     # 필수 — 미입력 등록 차단 (FR-51)
    effective_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="SCHEDULED")
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiDispatchQueue(Base):
    """AI 색인 발신 큐 (FR-52/53) — design §3.2."""
    __tablename__ = "ai_dispatch_queue"
    __table_args__ = (
        CheckConstraint("event_type IN ('CREATE','UPDATE','REVOKE')", name="ck_disp_event"),
        CheckConstraint("filter_status IN ('PENDING','PASSED','BLOCKED','APPROVED')",
                        name="ck_disp_filter"),
        CheckConstraint("dispatch_status IN ('QUEUED','SENT','DONE','FAILED','DLQ')",
                        name="ck_disp_status"),
        {"schema": "regulatory"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    erp_ref_id: Mapped[uuid.UUID] = mapped_column()
    revision_no: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(8))
    urgency: Mapped[str] = mapped_column(String(10), default="BATCH")  # IMMEDIATE | BATCH
    client_request_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)
    filter_status: Mapped[str] = mapped_column(String(12), default="PENDING")
    filter_detail: Mapped[dict | None] = mapped_column(JSON)
    approved_hash: Mapped[str | None] = mapped_column(String(64))  # 승인 바인딩 (design §5.3)
    dispatch_status: Mapped[str] = mapped_column(String(10), default="QUEUED")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    poll_count: Mapped[int] = mapped_column(Integer, default=0)  # 상태폴링 횟수(백오프 지수)
    next_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))                                 # 백오프 — 이 시각 전 재폴링 금지
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_code: Mapped[int | None] = mapped_column(Integer)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)


class InstitutionGradeSetting(Base):
    """기관 종별 본인부담률 (D-6 정밀화) — 시점관리. 종별 변경=신규 행(effective_from).

    진료일 기준 유효 종별로 외래 본인부담률 적용 — 과거 진료는 당시 종별로 계산.
    """
    __tablename__ = "institution_grade_settings"
    __table_args__ = (
        CheckConstraint("grade IN ('CLINIC','HOSPITAL','GENERAL','TERTIARY')",
                        name="ck_inst_grade"),
        UniqueConstraint("effective_from", name="uq_inst_grade_from"),
        {"schema": "regulatory"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    grade: Mapped[str] = mapped_column(String(12))
    outpatient_rate: Mapped[float] = mapped_column(Numeric(4, 3))
    inpatient_rate: Mapped[float] = mapped_column(Numeric(4, 3), default=0.20)
    meal_rate: Mapped[float] = mapped_column(Numeric(4, 3), default=0.50)
    special_case_rate: Mapped[float] = mapped_column(Numeric(4, 3), default=0.05)
    effective_from: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class DrugCodeMap(Base):
    """약품 코드 매핑 (D-6) — HIS 비표준코드 ↔ 보험 제품코드 상시 레이어.

    HIS 약품 명칭/성분 → 약가 마스터 매칭 후보 → 약제팀 확정 → 자동 변환.
    한번 CONFIRMED면 이후 동일 his_code 라인은 insurance_code로 단가 해소.
    """
    __tablename__ = "drug_code_map"
    __table_args__ = (
        CheckConstraint("status IN ('SUGGESTED','CONFIRMED','REJECTED')", name="ck_dcm_status"),
        {"schema": "regulatory"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    his_code: Mapped[str] = mapped_column(String(20), unique=True)
    insurance_code: Mapped[str | None] = mapped_column(String(20))
    drug_name: Mapped[str | None] = mapped_column(String(200))
    ingredient: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10), default="SUGGESTED", index=True)
    confidence: Mapped[str | None] = mapped_column(String(8))   # HIGH|MEDIUM|LOW
    candidates: Mapped[list | None] = mapped_column(JSON)
    matched_name: Mapped[str | None] = mapped_column(String(200))
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class DrugStdCode(Base):
    """의약품 표준코드 마스터 (마이그 0163) — 심평원 약가마스터_의약품표준코드 공개파일 적재.

    표준코드(GS1 바코드 880…) ↔ 품목기준코드 ↔ 제품코드(보험 EDI) ↔ 성분코드 브리지.
    용도: ①약품 코드 매칭(drug_matcher) 표준코드 폴백 ②워크스테이션 바코드 스캔 식별
    ③전문일반·특수관리(마약류 등) 속성 참조. 적재는 scripts/import_drug_std_codes.py
    (버전 파일 전량 교체 — DB 판본은 source_file로 추적).
    """
    __tablename__ = "drug_std_codes"
    __table_args__ = {"schema": "regulatory"}

    std_code: Mapped[str] = mapped_column(String(14), primary_key=True)   # 표준코드(바코드)
    item_seq: Mapped[str | None] = mapped_column(String(12), index=True)  # 품목기준코드
    product_code: Mapped[str | None] = mapped_column(String(12), index=True)  # 제품코드(보험 EDI)
    ingredient_code: Mapped[str | None] = mapped_column(String(12))       # 일반명(성분명)코드
    name: Mapped[str] = mapped_column(String(200))                        # 한글상품명
    company: Mapped[str | None] = mapped_column(String(120))              # 업체명
    spec: Mapped[str | None] = mapped_column(String(120))                 # 약품규격
    pack_qty: Mapped[int | None] = mapped_column(Integer)                 # 제품총수량
    form: Mapped[str | None] = mapped_column(String(40))                  # 제형구분
    rx_otc: Mapped[str | None] = mapped_column(String(20))                # 전문일반구분
    rep_code: Mapped[str | None] = mapped_column(String(14))              # 대표코드
    atc: Mapped[str | None] = mapped_column(String(80))                   # ATC 코드
    special_class: Mapped[str | None] = mapped_column(String(60))         # 특수관리약품구분
    canceled_on: Mapped[date | None] = mapped_column(Date)                # 취소일자(폐지)
    source_file: Mapped[str | None] = mapped_column(String(120))          # 적재 원본 판본


class LaborRule(Base):
    """노동규제 임계값 master (FR-32) — 듀티 편성 시 위반 경고 기준.

    duty_service.DEFAULT_RULES(법 조문 기반 초안)를 DB로 이전(G3). 인사팀이 조항·임계값
    확정 시 행 수정. 미적재(빈 표)면 코드 기본값 폴백 → 하위호환. notice_ref=근거 조항.
    """
    __tablename__ = "labor_rules"
    __table_args__ = {"schema": "regulatory"}

    rule_key: Mapped[str] = mapped_column(String(40), primary_key=True)  # weekly_hours_general 등
    threshold: Mapped[float] = mapped_column(Numeric(6, 1))              # 임계값(시간/일수)
    notice_ref: Mapped[str | None] = mapped_column(String(120))         # 근거 조항(전공의법 §7① 등)
    active: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstitutionSurchargeRate(Base):
    """종별가산율 master(G6) — 종별×가산율, 적용대상 code_type. BASE_CATALOG 행위료에 ERP가 적용."""
    __tablename__ = "institution_surcharge_rates"
    __table_args__ = {"schema": "regulatory"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    grade: Mapped[str] = mapped_column(String(20), unique=True)
    rate: Mapped[float] = mapped_column(Numeric(4, 3))
    applies_to: Mapped[str] = mapped_column(String(40), default="TARIFF")
    active: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PatientSafetyIncident(Base):
    """환자안전사고 보고 (환자안전법 §14 자율보고) — 근접오류·위해사건·적신호사건 접수·검토·종결.

    임상 RCA는 HIS/임상 소관, ERP는 보고관리·분류·통계·재발방지 추적. 보고자 보호(자율보고)로
    reporter는 선택. severity는 위해 정도(NONE 근접오류·MILD·MODERATE·SEVERE·DEATH).
    """
    __tablename__ = "patient_safety_incidents"
    __table_args__ = (
        CheckConstraint("incident_type IN ('NEAR_MISS','ADVERSE','SENTINEL')",
                        name="ck_psi_type"),
        CheckConstraint("severity IN ('NONE','MILD','MODERATE','SEVERE','DEATH')",
                        name="ck_psi_severity"),
        CheckConstraint("status IN ('REPORTED','REVIEWING','ACTIONED','CLOSED')",
                        name="ck_psi_status"),
        {"schema": "regulatory"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    occurred_on: Mapped[date] = mapped_column(Date, index=True)       # 사고 발생일
    incident_type: Mapped[str] = mapped_column(String(12))           # NEAR_MISS|ADVERSE|SENTINEL
    severity: Mapped[str] = mapped_column(String(10), default="NONE")
    category: Mapped[str | None] = mapped_column(String(40))         # 투약·낙상·검사·수술·감염 등
    department_id: Mapped[int | None] = mapped_column(BigInteger)
    description: Mapped[str] = mapped_column(Text)                   # 사고 경위(비식별 권장)
    reporter_id: Mapped[int | None] = mapped_column(BigInteger)      # 자율보고 — 익명 가능(NULL)
    status: Mapped[str] = mapped_column(String(10), default="REPORTED")
    action: Mapped[str | None] = mapped_column(Text)               # 재발방지 조치
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RadiationDose(Base):
    """방사선 종사자 피폭선량 (진단용방사선 안전관리규칙) — 분기 측정·연 한도 추적. (emp,period) 고유."""
    __tablename__ = "radiation_doses"
    __table_args__ = (
        UniqueConstraint("employee_id", "period", name="uq_rad_dose"),
        {"schema": "regulatory"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(BigInteger, index=True)
    period: Mapped[str] = mapped_column(String(7))           # yyyy-Qn / yyyy-mm
    dose_msv: Mapped[float] = mapped_column(Numeric(7, 3))   # 피폭선량(mSv)
    instrument: Mapped[str | None] = mapped_column(String(60))
    measured_on: Mapped[date] = mapped_column(Date)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RadiationEquipment(Base):
    """방사선 장비 정기검사 — 검사일·만기 추적(만기 임박 알림)."""
    __tablename__ = "radiation_equipment"
    __table_args__ = ({"schema": "regulatory"},)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    equipment_no: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    last_inspected_on: Mapped[date | None] = mapped_column(Date)
    next_inspection_on: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InfectionIndicator(Base):
    """감염관리 지표 (의료기관 인증평가·감염관리) — 월별 지표를 분자/분모로 적재, 비율·목표대비 추적.

    지표유형: HAI_UTI(요로)·HAI_BSI(혈류)·HAI_SSI(수술부위)·HAI_VAP(인공호흡기폐렴)·
    HAND_HYGIENE(손위생 수행률)·ANTIBIOTIC(항생제 사용률/DDD)·MDRO(다제내성균 분리율).
    비율 = 분자/분모×배수(감염밀도=×1000 재원일, 수행률=×100). (period,type,dept) 고유.
    임상 판정·미생물 결과는 HIS/진단검사 소관, ERP는 지표 집계·추세·목표관리·인증근거."""
    __tablename__ = "infection_indicators"
    __table_args__ = (
        CheckConstraint("indicator_type IN ('HAI_UTI','HAI_BSI','HAI_SSI','HAI_VAP',"
                        "'HAND_HYGIENE','ANTIBIOTIC','MDRO')", name="ck_inf_type"),
        UniqueConstraint("period", "indicator_type", "department_id", name="uq_inf_indicator"),
        {"schema": "regulatory"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(7), index=True)      # YYYY-MM
    indicator_type: Mapped[str] = mapped_column(String(16), index=True)
    department_id: Mapped[int | None] = mapped_column(BigInteger)   # NULL=전원(기관 전체)
    numerator: Mapped[float] = mapped_column(Numeric(14, 2))        # 감염건수·수행건수 등
    denominator: Mapped[float] = mapped_column(Numeric(14, 2))      # 재원일수·관찰기회 등
    multiplier: Mapped[int] = mapped_column(Integer, default=1000)  # 감염밀도×1000·수행률×100
    target: Mapped[float | None] = mapped_column(Numeric(10, 2))    # 목표치(초과 시 경보)
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class InstitutionProfile(Base):
    """기관 규제 프로필 (싱글턴 id=1) — 산정 엔진이 필요로 하는 기관 고정 파라미터의 SoT.

    간호등급 산정의 종별(hospital_type)·허가병상수 등. 미설정 시 서비스 effective()가 안전 기본값
    (HOSPITAL) 반환. 향후 다른 기관 고정 규제 파라미터 확장 자리.
    """
    __tablename__ = "institution_profile"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_institution_profile_singleton"),
        CheckConstraint(
            "hospital_type IN ('TERTIARY','GENERAL','HOSPITAL','CLINIC_BED_BASIS')",
            name="ck_institution_hospital_type"),
        {"schema": "regulatory"},
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=1)
    hospital_type: Mapped[str] = mapped_column(String(20), default="HOSPITAL")  # 종별(간호등급 구간)
    licensed_beds: Mapped[int | None] = mapped_column(Integer)   # 허가병상(의원급 병상기준 산정용)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class NursingCensus(Base):
    """분기 재원환자수 대장 — 간호등급 산정 재원일수 실측치(HIS 0시 census M0a 도착 전 수동 관리).

    산정기간(전전분기15~전분기14) 시작일을 자연키로. NursingMonitor가 수동 파라미터 다음 우선순위로 사용.
    """
    __tablename__ = "nursing_census"
    __table_args__ = (
        UniqueConstraint("period_start", name="uq_nursing_census_period"),
        {"schema": "regulatory"},
    )
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    patient_days: Mapped[float] = mapped_column(Numeric(12, 2))   # 재원일수 합
    note: Mapped[str | None] = mapped_column(String(200))
    entered_by: Mapped[int | None] = mapped_column(BigInteger)
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccreditationItem(Base):
    """의료기관 인증평가 조사항목 (상시준비) — 조사기준별 준비상태·근거·담당 관리.

    domain: BASIC(기본가치체계)·CARE(환자진료체계)·ADMIN(행정관리체계)·PERFORMANCE(성과관리체계).
    status: READY(준비완료)·PARTIAL(보완필요)·GAP(미비)·NA(해당없음). 근거(evidence)·담당부서·최종점검.
    지표(환자안전·감염관리·방사선·VOC 등)는 각 도메인 화면과 연계, 본 표는 조사항목 상시준비 등록부.
    """
    __tablename__ = "accreditation_items"
    __table_args__ = (
        CheckConstraint("domain IN ('BASIC','CARE','ADMIN','PERFORMANCE')", name="ck_acc_domain"),
        CheckConstraint("status IN ('READY','PARTIAL','GAP','NA')", name="ck_acc_status"),
        UniqueConstraint("standard", "item_code", name="uq_acc_item"),
        {"schema": "regulatory"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    standard: Mapped[str] = mapped_column(String(24), default="ACUTE")   # 인증기준 세트(급성기 등)
    domain: Mapped[str] = mapped_column(String(12), index=True)
    chapter: Mapped[str | None] = mapped_column(String(60))              # 장(예: 환자안전보장활동)
    item_code: Mapped[str] = mapped_column(String(24))                   # 조사항목 번호
    title: Mapped[str] = mapped_column(String(200))
    requirement: Mapped[str | None] = mapped_column(Text)                # 기준·조사방법
    evidence_note: Mapped[str | None] = mapped_column(Text)              # 근거·비치자료 위치
    status: Mapped[str] = mapped_column(String(10), default="GAP", index=True)
    owner_dept_id: Mapped[int | None] = mapped_column(BigInteger)
    last_reviewed_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
