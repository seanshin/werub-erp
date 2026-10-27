"""hr 스키마 모델 — design §3.5."""
from datetime import date, datetime, time

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer,
    LargeBinary, Numeric, String, Text, Time, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp.common.contract_lifecycle import STATUS_SQL as _CONTRACT_STATUS_SQL
from erp.common.months import ym_check_sql as _ym_check
from erp.db import Base, BigIntPK


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = {"schema": "hr"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_no: Mapped[str] = mapped_column(String(20), unique=True)  # 사번
    external_user_id: Mapped[str | None] = mapped_column(String(64), index=True)  # Clinic 직원 id(동기화·근태 매핑)
    email: Mapped[str | None] = mapped_column(String(255))   # SSO JIT 연결 키 (staff 이벤트 출처)
    name: Mapped[str] = mapped_column(String(80))
    birth_date: Mapped[date | None] = mapped_column(Date)
    phone_enc: Mapped[bytes | None] = mapped_column(LargeBinary)   # AES-GCM (design §6.4)
    address_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    department_id: Mapped[int | None] = mapped_column(BigInteger)  # common.departments (서비스 경유)
    position: Mapped[str | None] = mapped_column(String(40))       # 직급
    job_group: Mapped[str] = mapped_column(String(16), default="STAFF")
    # NURSE | DOCTOR | RESIDENT(전공의) | TECH | STAFF — 노동규제 룰 분기 (전공의법)
    # 인사 SoT 분리(D-2): HIS=의료진·정규직 마스터(미러·읽기전용) / ERP=비정규 직군 직접관리
    source: Mapped[str] = mapped_column(String(4), default="ERP")        # HIS | ERP
    staff_class: Mapped[str | None] = mapped_column(String(8))           # MEDICAL | SUPPORT
    job_category: Mapped[str | None] = mapped_column(String(24))         # DOCTOR·NURSE·RADIOTECH·SECURITY…
    employment_type: Mapped[str] = mapped_column(String(12), default="REGULAR")
    # REGULAR | CONTRACT | DAILY | TEMP | WATCH(감시단속·경비)
    contract_start: Mapped[date | None] = mapped_column(Date)            # 계약직 계약기간
    contract_end: Mapped[date | None] = mapped_column(Date)
    daily_wage: Mapped[float | None] = mapped_column(Numeric(12, 2))     # 일용직 일당
    pension_type: Mapped[str] = mapped_column(String(4), default="DB")   # 퇴직연금 DB|DC
    hired_on: Mapped[date] = mapped_column(Date)
    resigned_on: Mapped[date | None] = mapped_column(Date)         # 퇴직 후 3년 보존 기산
    access_revoked_on: Mapped[date | None] = mapped_column(Date)   # 퇴사자 ERP 계정 차단 시점(0251·1회)
    resign_reason: Mapped[str | None] = mapped_column(String(20))  # 퇴직 사유(자발/비자발/정년 등)
    # 간호등급 산정 정밀화 — 주 근로시간(단시간 환산 0.4~0.9·NULL=전일제 1.0)·산정 제외 사유
    weekly_hours: Mapped[float | None] = mapped_column(Numeric(4, 1))     # 주 근로시간(FTE 환산)
    nursing_exclude_reason: Mapped[str | None] = mapped_column(String(20))  # 간호감독/노조전임/호스피스 등(NULL=포함)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Appointment(Base):
    """발령 이력 (FR-03) — 부서 이동·승진."""
    __tablename__ = "appointments"
    __table_args__ = {"schema": "hr"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    appointment_type: Mapped[str] = mapped_column(String(16))  # TRANSFER | PROMOTION | HIRE | RESIGN
    from_department_id: Mapped[int | None] = mapped_column(BigInteger)
    to_department_id: Mapped[int | None] = mapped_column(BigInteger)
    from_position: Mapped[str | None] = mapped_column(String(40))
    to_position: Mapped[str | None] = mapped_column(String(40))
    effective_on: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)


class License(Base):
    """자격·면허 (FR-04) — 만료 알림 D-30/D-7 + 권한 플래그 연동 (FR-47)."""
    __tablename__ = "licenses"
    __table_args__ = {"schema": "hr"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    license_type: Mapped[str] = mapped_column(String(40))   # 의사·간호사·약사·방사선사...
    license_no: Mapped[str] = mapped_column(String(40))
    issued_on: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)   # NULL = 무기한
    training_completed_on: Mapped[date | None] = mapped_column(Date)  # 보수교육 이수


class LeaveGrant(Base):
    """연차 부여 (FR-31 — 근로기준법 제60조 산정 결과)."""
    __tablename__ = "leave_grants"
    __table_args__ = (
        UniqueConstraint("employee_id", "grant_year", name="uq_leave_grant_year"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    grant_year: Mapped[int] = mapped_column(Integer)         # 부여 연도
    granted_days: Mapped[float] = mapped_column(Numeric(4, 1))
    basis: Mapped[str] = mapped_column(String(200))          # 산정 근거 (감사 추적)


class LeaveRequest(Base):
    """휴가 신청 워크플로우 (FR-05)."""
    __tablename__ = "leave_requests"
    __table_args__ = (
        CheckConstraint("status IN ('REQUESTED','APPROVED','REJECTED','CANCELED')",
                        name="ck_leave_status"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    leave_type: Mapped[str] = mapped_column(String(16), default="ANNUAL")  # ANNUAL|SICK|SPECIAL
    start_on: Mapped[date] = mapped_column(Date)
    end_on: Mapped[date] = mapped_column(Date)
    days: Mapped[float] = mapped_column(Numeric(4, 1))
    status: Mapped[str] = mapped_column(String(12), default="REQUESTED")
    reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_id: Mapped[str | None] = mapped_column(String(64))       # HIS W.Sign 문서ID
    view_url: Mapped[str | None] = mapped_column(String(300))         # 결재 문서 딥링크
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DutySchedule(Base):
    """월 듀티 헤더 (FR-06)."""
    __tablename__ = "duty_schedules"
    __table_args__ = (
        UniqueConstraint("department_id", "yyyymm", name="uq_duty_month"),
        CheckConstraint(_ym_check("yyyymm"), name="ck_duty_yyyymm"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    department_id: Mapped[int] = mapped_column(BigInteger)
    yyyymm: Mapped[str] = mapped_column(String(7))           # 2026-07
    status: Mapped[str] = mapped_column(String(12), default="DRAFT")  # DRAFT | PUBLISHED
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ShiftType(Base):
    """근무 시프트 카탈로그 (FR-32) — 코드별 근무시간·야간/근무일 플래그.

    노동규제 룰(주간시간·전공의 4주평균·연속야간/근무일)이 시프트당 시간을 이 표에서 조회.
    D/E/N/OFF 기본(8/8/8/0) 외 당직·온콜 등 장시간 시프트를 인사팀이 등록하면 80h 룰이 실효화.
    빈 표(미적재)면 duty_service의 코드 기본값으로 폴백 → 무영향(하위호환).
    """
    __tablename__ = "shift_types"
    __table_args__ = {"schema": "hr"}

    code: Mapped[str] = mapped_column(String(4), primary_key=True)   # D|E|N|OFF|OC(당직)…
    label: Mapped[str] = mapped_column(String(40))
    work_hours: Mapped[float] = mapped_column(Numeric(4, 1), default=0)  # 1근무당 시간(당직 24h 등)
    is_night: Mapped[bool] = mapped_column(Boolean, default=False)        # 연속야간 룰 대상
    counts_as_workday: Mapped[bool] = mapped_column(Boolean, default=True)  # 연속근무일 룰 대상(OFF=False)
    # 근태 지각/조퇴 판정 기준 시각(FR-29) — 미설정 시 attendance 코드 기본값 폴백.
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DutyAssignment(Base):
    """일·직원별 근무 배정 — 시프트 코드는 hr.shift_types 카탈로그 검증 (FR-06/32)."""
    __tablename__ = "duty_assignments"
    __table_args__ = (
        UniqueConstraint("schedule_id", "employee_id", "duty_on", name="uq_duty_cell"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("hr.duty_schedules.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"))
    duty_on: Mapped[date] = mapped_column(Date)
    shift: Mapped[str] = mapped_column(String(3))


class PayrollRecord(Base):
    """급여 헤더 (FR-07a) — 확정 후 수정 시 감사."""
    __tablename__ = "payroll_records"
    __table_args__ = (
        UniqueConstraint("employee_id", "yyyymm", name="uq_payroll_month"),
        CheckConstraint(_ym_check("yyyymm"), name="ck_payroll_yyyymm"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    yyyymm: Mapped[str] = mapped_column(String(7))
    paid_on: Mapped[date | None] = mapped_column(Date)   # 임금 지급일(임금명세서 법정 §27의2 2호·P0)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger)


class PayrollSettings(Base):
    """급여 정책 설정 (싱글턴 id=1) — 관리화면(/admin/payroll-settings)에서 ADMIN이 설정.

    성과급(INCENTIVE) 원천징수 방식은 세무 정책이라 하드코딩하지 않고 기관이 설정한다.
    NONE(기본·현행·연말정산 정산) / FLAT(정률 원천징수). employer_burden_posting=사업주부담 GL 전기(후속).
    """
    __tablename__ = "payroll_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_payroll_settings_singleton"),
        CheckConstraint("incentive_withholding_method IN ('NONE','FLAT','COMBINED')",
                        name="ck_payroll_wh_method"),
        CheckConstraint("night_pay_mode IN ('FLAT','STATUTORY','MAX')",
                        name="ck_payroll_night_mode"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=1)
    incentive_withholding_method: Mapped[str] = mapped_column(String(10), default="NONE")
    # 야간수당 산정(M-11): FLAT=정액단가(현행·NIGHT_HOURLY) | STATUTORY=통상시급×0.5 가산(근기법§56③)
    # | MAX=둘 중 큰 값(법정 미달 방지)
    night_pay_mode: Mapped[str] = mapped_column(String(10), default="FLAT")
    incentive_withholding_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0)  # FLAT 정률(0~1)
    combined_months: Mapped[int] = mapped_column(Integer, default=1)   # COMBINED 상여 지급대상기간(개월)
    # 인사평가 등급 → 성과급 배율 적용(0232·P1-1). 기본 OFF=현행 동일. 배율맵은 코드 상수.
    incentive_grade_link: Mapped[bool] = mapped_column(Boolean, default=False)
    employer_burden_posting: Mapped[bool] = mapped_column(Boolean, default=False)
    employer_ei_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0.0115)  # 고용보험 사업주 요율
    employer_wc_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0.007)   # 산재 요율(업종별)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), onupdate=func.now())


class PayItemCatalog(Base):
    """급여 항목코드 마스터 (P0) — 원본코드→한글명·과세구분·표시순서 SoT(임금명세서 표기).

    현 PayrollLine.item_code는 원본코드(BASE·OT·NP…)만 저장 → 명세서가 코드를 그대로 노출.
    이 카탈로그로 한글 항목명·과세·표시순을 단일화. 미등록 코드는 렌더가 원본코드로 폴백(정직).
    """
    __tablename__ = "pay_item_catalog"
    __table_args__ = (
        CheckConstraint("kind IN ('EARNING','DEDUCTION')", name="ck_pay_item_kind"),
        {"schema": "hr"},
    )

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    name_en: Mapped[str | None] = mapped_column(String(60))       # 영문(국제병원 영문명세·P3)
    kind: Mapped[str] = mapped_column(String(10))                 # EARNING | DEDUCTION
    category: Mapped[str | None] = mapped_column(String(20))
    taxable: Mapped[bool] = mapped_column(Boolean, default=True)
    nontax_limit: Mapped[float | None] = mapped_column(Numeric(12, 0))  # 비과세 한도(식대 월20만 등)
    display_order: Mapped[int] = mapped_column(Integer, default=100)
    is_medical: Mapped[bool] = mapped_column(Boolean, default=False)    # 의료진 전용(P2)


class PayrollLine(Base):
    """급여 라인 — JSONB 대신 라인 테이블 (집계·검증·감사, design v0.3 ⑧)."""
    __tablename__ = "payroll_lines"
    __table_args__ = (
        CheckConstraint("item_kind IN ('EARNING','DEDUCTION')", name="ck_payroll_kind"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("hr.payroll_records.id"), index=True)
    item_code: Mapped[str] = mapped_column(String(20))   # BASE|OT|NIGHT|MEAL|TAX|INSURANCE...
    item_kind: Mapped[str] = mapped_column(String(10))
    amount: Mapped[float] = mapped_column(Numeric(12, 0))
    # 계산근거(표시 전용·회계 SoT는 amount) — 임금명세서 계산방법(§27의2 5호). {formula, ...구조화}.
    # 없으면 명세서는 계산식 행 생략(과거 라인·소급 데이터 정직 보호).
    calc_basis: Mapped[dict | None] = mapped_column(JSON)


class RateSettlement(Base):
    """요율 차액 정산 (0260) — 낮은 요율로 확정·지급된 급여월의 4대보험 차액을 어떻게 처리했는지(결정+실행 한 묶음).

    2026-10-10: 2026 코드 요율이 2025 복사본이라 확정·지급된 급여의 근로자 공제가 과소. 방식
    NEXT_PAYROLL_ADJUST(다음 급여 추가 공제) | EMPLOYER_ABSORB(기관 부담 전표) | NOT_PAYROLL_SOT(기록만).
    실행 시점 직원별 차액을 items로 고정(이후 요율 재등록에 흔들리지 않게). 급여월당 ACTIVE 1건.
    """
    __tablename__ = "rate_settlements"
    __table_args__ = (
        CheckConstraint("method IN ('NEXT_PAYROLL_ADJUST','EMPLOYER_ABSORB','NOT_PAYROLL_SOT')",
                        name="ck_rate_settle_method"),
        CheckConstraint("status IN ('ACTIVE','CANCELED')", name="ck_rate_settle_status"),
        CheckConstraint(_ym_check("source_yyyymm"), name="ck_rate_settle_source"),
        Index("uq_rate_settle_active", "source_yyyymm", unique=True,
              postgresql_where=text("status = 'ACTIVE'"), sqlite_where=text("status = 'ACTIVE'")),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source_yyyymm: Mapped[str] = mapped_column(String(7))
    method: Mapped[str] = mapped_column(String(24))
    target_yyyymm: Mapped[str | None] = mapped_column(String(7))       # NEXT_PAYROLL_ADJUST만
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE")
    decision_key: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(String(1000))
    employee_total: Mapped[float] = mapped_column(Numeric(14, 0), default=0)   # 근로자분 차액 합
    employer_total: Mapped[float] = mapped_column(Numeric(14, 0), default=0)   # 사업주분(연금·건강·장기요양) 차액 합
    absorbed_total: Mapped[float] = mapped_column(Numeric(14, 0), default=0)   # 기관이 대신 부담한 근로자분
    voucher_ref: Mapped[str | None] = mapped_column(String(64))
    rates: Mapped[dict | None] = mapped_column(JSON)                           # 실행 시점 요율 스냅샷
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    canceled_by: Mapped[int | None] = mapped_column(BigInteger)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(500))


class RateSettlementItem(Base):
    """요율 차액 정산 직원별 항목 — 근로자분 차액(항목별)·적용 급여(다음 급여 공제 시)."""
    __tablename__ = "rate_settlement_items"
    __table_args__ = (
        UniqueConstraint("settlement_id", "employee_id", name="uq_rate_settle_item"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey("hr.rate_settlements.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    source_record_id: Mapped[int] = mapped_column(BigInteger)
    np: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    hi: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    ltc: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    ei: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    disposition: Mapped[str] = mapped_column(String(12))     # DEDUCT(다음 급여) | ABSORB(기관) | RECORD(기록만)
    applied_record_id: Mapped[int | None] = mapped_column(BigInteger)   # 공제가 확정된 급여
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WithholdingTable(Base):
    """근로소득 간이세액표 (0262·소득세법 시행령 별표 2) — 시행일별 버전. CANDIDATE(추출 후보)→ACTIVE(사람 대조·출처)→RETIRED.

    2026-10-10: 월 원천징수가 표가 아닌 근사 산식이라 확정 급여가 과다 원천징수됐다. 표 값은 법정 데이터라
    사람이 홈택스 원표와 대조해 활성화한 것만 계산에 쓴다(활성 표가 없는 기간은 종전 산식·명세서에 '근사 산식' 표기).
    rows=[[lo_k, hi_k, f1..f11], …](월급여액 천원 이상·미만, 공제대상가족 수별 세액) · 1천만원 초과는 별표 계산식(코드·시행일별).
    """
    __tablename__ = "withholding_tables"
    __table_args__ = (
        CheckConstraint("status IN ('CANDIDATE','ACTIVE','RETIRED')", name="ck_wht_status"),
        Index("uq_wht_active", "effective_from", unique=True,
              postgresql_where=text("status = 'ACTIVE'"), sqlite_where=text("status = 'ACTIVE'")),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    effective_from: Mapped[date] = mapped_column(Date)          # 시행일(이후 지급분)
    status: Mapped[str] = mapped_column(String(10), default="CANDIDATE")
    title: Mapped[str] = mapped_column(String(120))
    source_note: Mapped[str | None] = mapped_column(String(500))   # 활성화 시 사람이 적는 대조 근거(필수)
    rows: Mapped[list] = mapped_column(JSON)
    child_deduction: Mapped[dict] = mapped_column(JSON)             # {"one":20830,"two":45830,"extra":33330}
    checksum: Mapped[str] = mapped_column(String(64))               # rows sha256(변조·중복 등록 판별)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    activated_by: Mapped[int | None] = mapped_column(BigInteger)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AttendanceSummary(Base):
    """월 근태 집계 (FR-29) — 그룹웨어 수신 기반 일배치 산출."""
    __tablename__ = "attendance_summaries"
    __table_args__ = (
        UniqueConstraint("employee_id", "yyyymm", name="uq_attendance_month"),
        CheckConstraint(_ym_check("yyyymm"), name="ck_attendance_yyyymm"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    yyyymm: Mapped[str] = mapped_column(String(7))
    work_days: Mapped[int] = mapped_column(Integer, default=0)
    late_count: Mapped[int] = mapped_column(Integer, default=0)
    early_leave_count: Mapped[int] = mapped_column(Integer, default=0)
    night_shift_count: Mapped[int] = mapped_column(Integer, default=0)
    total_work_minutes: Mapped[int] = mapped_column(Integer, default=0)


class GroupwareAttendanceStaging(Base):
    """그룹웨어(Clinic) 근태 수신 스테이징 (integration 스키마) — raw 보존·멱등 (design §5.2).

    수신: RestGroupwareAttendanceSource → 소비: scheduler.job_attendance_summary →
    hr.attendance_summaries upsert. source_ref 멱등키로 재수집 시 중복 차단."""
    __tablename__ = "groupware_attendance_staging"
    __table_args__ = (
        UniqueConstraint("source_ref", name="uq_gw_attendance_source_ref"),  # 멱등키
        CheckConstraint("status IN ('NEW','APPLIED','FAILED')", name="ck_gw_att_status"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source_ref: Mapped[str] = mapped_column(String(80))            # f"{external_user_id}:{work_on}"
    external_user_id: Mapped[str] = mapped_column(String(64))      # Clinic userId (원천 식별자)
    employee_no: Mapped[str | None] = mapped_column(String(20))    # 매핑된 ERP 사번 (없으면 보류)
    name: Mapped[str | None] = mapped_column(String(80))           # 대사용 메타 (코어 미사용)
    department: Mapped[str | None] = mapped_column(String(80))
    clock_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clock_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    work_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="NEW")
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CertificateIssue(Base):
    """증명서 발급 이력 (FR-23) — 반출 기록."""
    __tablename__ = "certificate_issues"
    __table_args__ = (
        CheckConstraint("cert_type IN ('EMPLOYMENT','CAREER')", name="ck_cert_type"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    cert_type: Mapped[str] = mapped_column(String(12))
    issued_to: Mapped[int] = mapped_column(BigInteger)   # 요청자 user_id
    purpose: Mapped[str | None] = mapped_column(String(200))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MedicalStaffContract(Base):
    """의료진 개별 계약 (P4) — 계약별 기본급·성과급 산식·당직. 직원당 ACTIVE 1건(temporal).

    성과급 산식: incentive_type + incentive_rate(%) + incentive_params(JSON: target·cap·floor·
    include_noncovered·deduct_adjustment). 실제 산정은 P4-3 엔진. 보수=민감(RBAC·암호화 대상).
    """
    __tablename__ = "medical_contracts"
    __table_args__ = (
        CheckConstraint("incentive_type IN ('NONE','NET_COLLECTION','DEPT_SHARE',"
                        "'TARGET_EXCESS','RVU')", name="ck_mc_incentive"),
        CheckConstraint(f"status IN ({_CONTRACT_STATUS_SQL})", name="ck_mc_status"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)        # 갱신·종료 시 설정
    effective_on: Mapped[date | None] = mapped_column(Date)    # 발효일(기본=valid_from)
    base_pay: Mapped[float] = mapped_column(Numeric(12, 0))    # 계약 월 기본급
    duty_pay: Mapped[float | None] = mapped_column(Numeric(10, 0))  # 당직 1회 단가(계약별)
    incentive_type: Mapped[str] = mapped_column(String(16), default="NONE")
    incentive_rate: Mapped[float | None] = mapped_column(Numeric(5, 2))  # 배분율 %
    incentive_params: Mapped[dict] = mapped_column(JSON, default=dict)
    exclusive: Mapped[bool] = mapped_column(Boolean, default=True)  # 전속/비전속
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")   # 공통 생애주기 커널
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminate_reason: Mapped[str | None] = mapped_column(String(200))
    terminated_on: Mapped[date | None] = mapped_column(Date)
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


class IncentiveCalculation(Base):
    """성과급 산정·승인 (P4-4) — 산정(DRAFT)→검토→승인(급여·전표 반영)→취소(claw-back).

    근거 snapshot(실적·산식) 보존. 직원·기간 1건. 승인 시 PayrollLine(INCENTIVE)+미지급 전표.
    """
    __tablename__ = "incentive_calculations"
    __table_args__ = (
        UniqueConstraint("employee_id", "period", name="uq_incentive_period"),
        CheckConstraint(_ym_check("period"), name="ck_incentive_period"),
        CheckConstraint("status IN ('DRAFT','REVIEWED','APPROVED','CANCELED')",
                        name="ck_incentive_status"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))                  # YYYY-MM
    contract_id: Mapped[int | None] = mapped_column(BigInteger)
    net_revenue: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    amount: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    status: Mapped[str] = mapped_column(String(10), default="DRAFT")
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)      # 근거(실적·산식)
    voucher_ref: Mapped[str | None] = mapped_column(String(40))     # 미지급 전표 source_ref(최근 전기)
    posting_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 전기 시퀀스 — 취소→재승인 시 신규 전표(멱등 충돌 방지)
    note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RetirementProvision(Base):
    """퇴직급여충당부채 월별 적립 — 직원×기간 추계액·증가분(전입액)·누적. 멱등(직원,기간)."""
    __tablename__ = "retirement_provisions"
    __table_args__ = (
        UniqueConstraint("employee_id", "period", name="uq_retire_prov"),
        CheckConstraint(_ym_check("period"), name="ck_retire_prov_period"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))          # yyyymm
    avg_wage: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    estimate: Mapped[float] = mapped_column(Numeric(16, 0), default=0)    # 추계액
    increment: Mapped[float] = mapped_column(Numeric(16, 0), default=0)   # 당월 전입액
    cumulative: Mapped[float] = mapped_column(Numeric(16, 0), default=0)  # 누적 충당
    voucher_ref: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ActuarialAssumption(Base):
    """확정급여채무(DBO) 보험수리 가정(K-IFRS 1019) — 할인율·임금상승률·정년. valid_from 시점관리."""
    __tablename__ = "actuarial_assumptions"
    __table_args__ = {"schema": "hr"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    discount_rate: Mapped[float] = mapped_column(Numeric(6, 4))     # 할인율
    salary_growth: Mapped[float] = mapped_column(Numeric(6, 4))     # 임금상승률
    retirement_age: Mapped[int] = mapped_column(Integer, default=60)  # 정년
    valid_from: Mapped[date] = mapped_column(Date, index=True)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LeaveAbsence(Base):
    """휴직/복직 (P1-①) — 육아·출산·병가·산재·일반. return_on NULL = 휴직 중.

    휴직 중: 급여 일괄생성 제외, 4대보험 납부예외 식별(insurance_exempt).
    근속(퇴직금)은 법령상 휴직 포함 — 충당 적립 변경 없음.
    """
    __tablename__ = "leave_absences"
    __table_args__ = (
        CheckConstraint("kind IN ('MATERNITY','PARENTAL','SICK','INDUSTRIAL','PERSONAL')",
                        name="ck_absence_kind"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12))
    start_on: Mapped[date] = mapped_column(Date)
    expected_return_on: Mapped[date | None] = mapped_column(Date)
    return_on: Mapped[date | None] = mapped_column(Date)
    insurance_exempt: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InsuranceReportEvent(Base):
    """4대보험 취득·상실 신고 추적 (P1-③) — 사유발생 자동적재·기한·처리상태.

    ACQUIRE=취득(입사) | LOSE=상실(퇴사) | EXEMPT=납부예외(휴직) | RESUME=재개(복직).
    멱등: (employee_id, event, basis_on) UNIQUE.
    """
    __tablename__ = "insurance_report_events"
    __table_args__ = (
        UniqueConstraint("employee_id", "event", "basis_on", name="uq_ins_report_event"),
        CheckConstraint("event IN ('ACQUIRE','LOSE','EXEMPT','RESUME')", name="ck_ins_event"),
        CheckConstraint("status IN ('PENDING','REPORTED')", name="ck_ins_status"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    event: Mapped[str] = mapped_column(String(8))
    basis_on: Mapped[date] = mapped_column(Date)            # 사유발생일(입사·퇴사·휴직·복직일)
    due_on: Mapped[date] = mapped_column(Date)              # 신고 기한
    status: Mapped[str] = mapped_column(String(10), default="PENDING")
    reported_on: Mapped[date | None] = mapped_column(Date)
    reported_by: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PayRateSetting(Base):
    """수당 단가 마스터 (P2-②·거버넌스 기간화) — 단협·취업규칙 단가를 DB로(코드 수정 없이 조정).

    (rate_key, valid_from) 기간 이력 — 연 단위 단협 갱신 시 새 발효행 추가(기존 보존·불변 로그).
    급여 생성은 해당 급여월 기준 유효 단가를 사용(단가 개정이 과거 급여 재생성을 왜곡하지 않게).
    미설정 키는 payroll_rules 코드 기본값 사용(하위호환).
    """
    __tablename__ = "pay_rate_settings"
    __table_args__ = (
        UniqueConstraint("rate_key", "valid_from", name="uq_pay_rate_period"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    rate_key: Mapped[str] = mapped_column(String(40), index=True)
    valid_from: Mapped[date] = mapped_column(Date, default=date(2020, 1, 1))  # 발효일(기간 이력)
    amount: Mapped[float] = mapped_column(Numeric(12, 0))
    description: Mapped[str | None] = mapped_column(String(120))
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class OffboardingSettlement(Base):
    """퇴직 정산 (P2-①) — 연차수당+퇴직금+충당상계를 한 절차로. 직원당 1회 멱등."""
    __tablename__ = "offboarding_settlements"
    __table_args__ = {"schema": "hr"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), unique=True)
    resigned_on: Mapped[date] = mapped_column(Date)
    leave_remaining_days: Mapped[float] = mapped_column(Numeric(4, 1), default=0)
    leave_allowance: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    avg_monthly_wage: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    severance_amount: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    provision_offset: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    # 원천징수 세액(0253) — 2026-10-09 전체 흐름 검토 2차 C: 정산 전표 2150엔 잡히나 신고 집계(A21)·
    # 원천징수영수증에 반영할 근거가 없었다. 0253 이전 정산분은 NULL(세액 미보관).
    retirement_income_tax: Mapped[float | None] = mapped_column(Numeric(14, 0))
    retirement_local_tax: Mapped[float | None] = mapped_column(Numeric(14, 0))
    leave_allowance_tax: Mapped[float | None] = mapped_column(Numeric(14, 0))
    leave_allowance_local_tax: Mapped[float | None] = mapped_column(Numeric(14, 0))
    voucher_ref: Mapped[str | None] = mapped_column(String(64))
    settled_by: Mapped[int | None] = mapped_column(BigInteger)
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompensationProfile(Base):
    """급여 기초정보(보수 프로필) — 직원별 기본급·부양가족·계좌(암호화).

    HIS/Clinic 동기화로 생성된 직원의 ERP 보완입력(온보딩) 대상 식별 기준.
    급여 generate가 월별 입력 대신 이 마스터를 기본값으로 사용.
    """
    __tablename__ = "compensation_profiles"
    __table_args__ = {"schema": "hr"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), unique=True, index=True)
    base_pay: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    dependents: Mapped[int] = mapped_column(Integer, default=1)
    bank_name: Mapped[str | None] = mapped_column(String(40))
    bank_code: Mapped[str | None] = mapped_column(String(4))              # 표준 은행코드(이체파일·R-2)
    bank_account_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # AES-GCM (design §6.4)
    resident_id_enc: Mapped[bytes | None] = mapped_column(LargeBinary)   # 주민번호 AES-GCM(HIS 인입·R-1)
    account_holder: Mapped[str | None] = mapped_column(String(40))       # 예금주(본인일치 검증용·R-2)
    effective_from: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ComplianceRecord(Base):
    """법정의무교육·건강검진 이수 기록 (P3) — 주기별 만료 추적(인증평가·노동법)."""
    __tablename__ = "compliance_records"
    __table_args__ = (
        UniqueConstraint("employee_id", "category", "done_on", name="uq_compliance"),
        CheckConstraint("kind IN ('TRAINING','CHECKUP')", name="ck_compliance_kind"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    category: Mapped[str] = mapped_column(String(24))
    done_on: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date] = mapped_column(Date, index=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dependent(Base):
    """부양가족 (P3) — 인적공제·간이세액 부양가족 수의 원천. counted=담당자 확정.

    암호화 검토(점검 M-4, 2026-06-12 결정): 성명·생년월일 **평문 유지** —
    기존 PII 분류 기준(직원·환자도 성명/생년월일 평문, 연락처·주소·계좌만 암호화)과
    일관. 주민번호류 도입 시 재검토. 조회는 hr.compensation read(ADMIN/DIRECTOR)+접속기록.
    """
    __tablename__ = "dependents"
    __table_args__ = (
        CheckConstraint("relation IN ('SPOUSE','CHILD','PARENT','GRANDPARENT',"
                        "'SIBLING','OTHER')", name="ck_dependent_relation"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    relation: Mapped[str] = mapped_column(String(12))
    birth_date: Mapped[date | None] = mapped_column(Date)
    disabled: Mapped[bool] = mapped_column(default=False)
    counted: Mapped[bool] = mapped_column(default=True)    # 공제 인원 산입(담당자 확정)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TrainingCourse(Base):
    """원내 교육 과정 마스터 (Tier3·0228) — 임의 사내 과정. 법정교육(compliance_records)과 별개.

    cycle_months=0 → 일회성(만료 없음). target=ALL/JOB_CATEGORY/DEPT(target_value로 대상 한정).
    """
    __tablename__ = "training_courses"
    __table_args__ = (
        UniqueConstraint("code", name="uq_training_course_code"),
        CheckConstraint(
            "category IN ('STATUTORY','JOB','SAFETY','INFECTION','ONBOARDING','QUALITY','OTHER')",
            name="ck_tcourse_category"),
        CheckConstraint("target IN ('ALL','JOB_CATEGORY','DEPT')", name="ck_tcourse_target"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    title: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(16), default="JOB")
    cycle_months: Mapped[int] = mapped_column(Integer, default=0)   # 0=일회성
    target: Mapped[str] = mapped_column(String(16), default="ALL")
    target_value: Mapped[str | None] = mapped_column(String(40))
    mandatory: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TrainingSession(Base):
    """교육 회차 — 과정의 개최 차수(개최일·시간·강사·방식)."""
    __tablename__ = "training_sessions"
    __table_args__ = (
        CheckConstraint("method IN ('OFFLINE','ONLINE','EXTERNAL')", name="ck_tsession_method"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("hr.training_courses.id"), index=True)
    held_on: Mapped[date] = mapped_column(Date, index=True)
    hours: Mapped[float] = mapped_column(Numeric(4, 1), default=1)
    method: Mapped[str] = mapped_column(String(10), default="OFFLINE")
    instructor: Mapped[str | None] = mapped_column(String(80))
    location: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TrainingCompletion(Base):
    """이수 기록 — 직원×회차(멱등). 주기 만료(valid_until)·점수·수료증번호."""
    __tablename__ = "training_completions"
    __table_args__ = (
        UniqueConstraint("session_id", "employee_id", name="uq_training_completion"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("hr.training_sessions.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("hr.training_courses.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    completed_on: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date, index=True)   # 일회성=NULL
    score: Mapped[float | None] = mapped_column(Numeric(5, 1))
    cert_no: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalCycle(Base):
    """인사평가 차수 (Tier3·0229) — 연간/반기·항목 템플릿·상태(DRAFT→OPEN→CLOSED)."""
    __tablename__ = "eval_cycles"
    __table_args__ = (
        UniqueConstraint("period", name="uq_eval_cycle_period"),
        CheckConstraint("cycle_type IN ('ANNUAL','HALF','QUARTER')", name="ck_evcycle_type"),
        CheckConstraint("status IN ('DRAFT','OPEN','CLOSED')", name="ck_evcycle_status"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(16), unique=True)
    title: Mapped[str] = mapped_column(String(160))
    cycle_type: Mapped[str] = mapped_column(String(10), default="ANNUAL")
    start_on: Mapped[date | None] = mapped_column(Date)
    end_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="DRAFT", index=True)
    grade_bands: Mapped[list | None] = mapped_column(JSON)   # [{grade,min}] 총점→등급
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalCriterion(Base):
    """평가 항목 — 차수별 역량/실적/MBO 문항·가중치·만점."""
    __tablename__ = "eval_criteria"
    __table_args__ = (
        CheckConstraint("kind IN ('COMPETENCY','PERFORMANCE','MBO')", name="ck_evcrit_kind"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("hr.eval_cycles.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12), default="COMPETENCY")
    title: Mapped[str] = mapped_column(String(160))
    weight: Mapped[float] = mapped_column(Numeric(5, 2), default=1)
    max_score: Mapped[float] = mapped_column(Numeric(5, 1), default=100)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Evaluation(Base):
    """평가 — 대상직원×차수. 상태 PENDING→SELF_DONE→EVAL_DONE→CONFIRMED. 가중 총점·등급."""
    __tablename__ = "evaluations"
    __table_args__ = (
        UniqueConstraint("cycle_id", "employee_id", name="uq_evaluation"),
        CheckConstraint("status IN ('PENDING','SELF_DONE','EVAL_DONE','CONFIRMED')",
                        name="ck_eval_status"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("hr.eval_cycles.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    evaluator_id: Mapped[int | None] = mapped_column(BigInteger)   # 1차 평가자(user id)
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)
    self_total: Mapped[float | None] = mapped_column(Numeric(6, 2))
    eval_total: Mapped[float | None] = mapped_column(Numeric(6, 2))
    final_grade: Mapped[str | None] = mapped_column(String(4))
    evaluator_comment: Mapped[str | None] = mapped_column(Text)
    self_comment: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalScore(Base):
    """평가×항목 점수 — 자기점수·평가자점수."""
    __tablename__ = "eval_scores"
    __table_args__ = (
        UniqueConstraint("evaluation_id", "criterion_id", name="uq_eval_score"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("hr.evaluations.id"), index=True)
    criterion_id: Mapped[int] = mapped_column(ForeignKey("hr.eval_criteria.id"))
    self_score: Mapped[float | None] = mapped_column(Numeric(5, 1))
    eval_score: Mapped[float | None] = mapped_column(Numeric(5, 1))
    note: Mapped[str | None] = mapped_column(String(300))


class BenefitProgram(Base):
    """복리후생 제도 (Tier3·0230) — 복지포인트·경조사·자기계발·검진지원 등. 연 한도·대상."""
    __tablename__ = "benefit_programs"
    __table_args__ = (
        UniqueConstraint("code", name="uq_benefit_program_code"),
        CheckConstraint("benefit_type IN ('POINT','SUBSIDY','CONGRATS','EVENT','OTHER')",
                        name="ck_bprogram_type"),
        CheckConstraint("unit IN ('KRW','POINT')", name="ck_bprogram_unit"),
        CheckConstraint("target IN ('ALL','JOB_CATEGORY','DEPT')", name="ck_bprogram_target"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    title: Mapped[str] = mapped_column(String(160))
    benefit_type: Mapped[str] = mapped_column(String(12), default="POINT")
    unit: Mapped[str] = mapped_column(String(8), default="KRW")
    annual_limit: Mapped[float] = mapped_column(Numeric(14, 0), default=0)   # 연 한도(직원당)
    target: Mapped[str] = mapped_column(String(16), default="ALL")
    target_value: Mapped[str | None] = mapped_column(String(40))
    requires_receipt: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BenefitGrant(Base):
    """직원×연도 부여 — 복지포인트/한도 적립(직원별 연 부여). 미부여 시 제도 annual_limit 적용."""
    __tablename__ = "benefit_grants"
    __table_args__ = (
        UniqueConstraint("program_id", "employee_id", "year", name="uq_benefit_grant"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("hr.benefit_programs.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    year: Mapped[int] = mapped_column(Integer)
    granted: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BenefitClaim(Base):
    """복리후생 신청 — 신청→승인→지급. 잔액 차감·한도 검증·지급 시 GL(5330/1110)."""
    __tablename__ = "benefit_claims"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_bclaim_amount"),
        CheckConstraint("status IN ('REQUESTED','APPROVED','PAID','REJECTED')",
                        name="ck_bclaim_status"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("hr.benefit_programs.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    claim_on: Mapped[date] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(10), default="REQUESTED", index=True)
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    paid_on: Mapped[date | None] = mapped_column(Date)
    voucher_ref: Mapped[str | None] = mapped_column(String(60))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmploymentContract(Base):
    """일반직(비의료진) 근로계약 (근기법 §17 근로조건 명시) — 임금·소정근로시간·근무일·취업장소·업무.

    의료진은 MedicalStaffContract(성과급). 본 계약은 일반직(SUPPORT) 근로조건 명시·이력 관리.
    직원당 ACTIVE 1건(신규 발효 시 기존 종료). DRAFT(작성)→ACTIVE(발효)→ENDED(종료). 계약직 갱신.
    """
    __tablename__ = "employment_contracts"
    __table_args__ = (
        CheckConstraint("contract_type IN ('REGULAR','FIXED_TERM','PART_TIME')",
                        name="ck_emp_contract_type"),
        CheckConstraint("status IN ('DRAFT','ACTIVE','ENDED')", name="ck_emp_contract_status"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    contract_type: Mapped[str] = mapped_column(String(12), default="REGULAR")
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)      # 정규직 무기한=NULL, 계약직=만료일
    status: Mapped[str] = mapped_column(String(10), default="DRAFT", index=True)
    # 근기법 §17 명시사항
    base_pay: Mapped[float] = mapped_column(Numeric(12, 0))          # 월 기본급
    weekly_hours: Mapped[float | None] = mapped_column(Numeric(5, 1))  # 주 소정근로시간
    work_schedule: Mapped[str | None] = mapped_column(String(200))   # 근무일·근로시간대(예 "월~금 09:00-18:00")
    work_location: Mapped[str | None] = mapped_column(String(120))   # 취업장소
    job_duties: Mapped[str | None] = mapped_column(String(200))      # 업무내용
    note: Mapped[str | None] = mapped_column(String(300))
    ended_on: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PayGradeStep(Base):
    """직급·호봉 체계 (호봉표) — (직급, 호봉, 연도)별 기본급 마스터. 급여 산정 참조·인사 기준.

    (grade, step, effective_year) 고유. 연도별 호봉표 개정을 append(신년도 행 추가).
    """
    __tablename__ = "pay_grade_steps"
    __table_args__ = (
        UniqueConstraint("grade", "step", "effective_year", name="uq_pay_grade_step"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    grade: Mapped[str] = mapped_column(String(24))          # 직급(예: 5급·주임·간호3급)
    step: Mapped[int] = mapped_column(Integer)             # 호봉
    base_pay: Mapped[float] = mapped_column(Numeric(12, 0))  # 월 기본급
    effective_year: Mapped[int] = mapped_column(Integer, index=True)
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DisciplinaryAction(Base):
    """징계·상벌 인사기록 (근기법·취업규칙) — 견책·감봉·정직·해임 / 표창·포상.

    action_type: DISCIPLINE(징계)·REWARD(포상). category=구체 종류. 의결일·효력기간·사유.
    """
    __tablename__ = "disciplinary_actions"
    __table_args__ = (
        CheckConstraint("action_type IN ('DISCIPLINE','REWARD')", name="ck_disc_type"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("hr.employees.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(12))
    category: Mapped[str] = mapped_column(String(24))       # 견책/감봉/정직/해임/강등 · 표창/포상
    reason: Mapped[str] = mapped_column(String(400))
    decided_on: Mapped[date] = mapped_column(Date, index=True)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobPosting(Base):
    """채용 공고 (ATS·Tier3) — 직무·인원·유형·상태. 지원자(JobApplicant) 상위."""
    __tablename__ = "job_postings"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','CLOSED','FILLED')", name="ck_posting_status"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160))
    department_id: Mapped[int | None] = mapped_column(BigInteger)
    employment_type: Mapped[str] = mapped_column(String(12), default="REGULAR")
    headcount: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(10), default="OPEN", index=True)
    posted_on: Mapped[date] = mapped_column(Date)
    close_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(400))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobApplicant(Base):
    """지원자 (ATS) — 공고별 지원·전형단계. 개인정보(이름·연락처)로 접근 한정(hr.recruit).

    stage: APPLIED→SCREENING→INTERVIEW→OFFER→HIRED / REJECTED(어느 단계서나). 개인정보 최소보관.
    """
    __tablename__ = "job_applicants"
    __table_args__ = (
        CheckConstraint("stage IN ('APPLIED','SCREENING','INTERVIEW','OFFER','HIRED','REJECTED')",
                        name="ck_applicant_stage"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    posting_id: Mapped[int] = mapped_column(ForeignKey("hr.job_postings.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    contact: Mapped[str | None] = mapped_column(String(120))     # 연락처/이메일(최소수집)
    source: Mapped[str | None] = mapped_column(String(40))       # 지원 경로(사람인·추천 등)
    stage: Mapped[str] = mapped_column(String(12), default="APPLIED", index=True)
    applied_on: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(400))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class StatutoryParam(Base):
    """법정 파라미터 관리화면 등록분(0250) — 매년 고시값을 코드 배포 없이 근거와 함께 등록.

    kind=INSURANCE: values = 4대보험 요율(statutory.INSURANCE_KEYS) — 코드 RATES보다 우선.
    kind=TAX_CARRY: values = {"from_year": N} — 세법 개정 없음을 확인하고 전년 소득세 파라미터 승계.
    kind=TAX_AMEND: values = {"from_year": N, "overrides": {키: 값}} — 세법 개정분만 원본 연도 위에 덮어씀(v1.300·0264).
    같은 연도에 TAX_CARRY·TAX_AMEND는 하나만(등록 시 다른 쪽 삭제).
    (year, kind) 1행 — 정정은 덮어쓰고 감사 기록(근거 source_note 필수).
    """
    __tablename__ = "statutory_params"
    __table_args__ = (
        UniqueConstraint("year", "kind", name="uq_statutory_params_year_kind"),
        CheckConstraint("kind IN ('INSURANCE','TAX_CARRY','TAX_AMEND')", name="ck_statutory_params_kind"),
        {"schema": "hr"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    year: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12))
    values: Mapped[dict] = mapped_column(JSON)
    source_note: Mapped[str] = mapped_column(String(500))     # 고시 번호·출처(필수)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
