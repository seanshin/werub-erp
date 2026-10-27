"""common 스키마 모델 — design §3.5."""
from datetime import date, datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp.db import Base, BigIntPK


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = {"schema": "common"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    code: Mapped[str | None] = mapped_column(String(8))   # 진료과 코드(claims.dept_code 매핑)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("common.departments.id"))
    head_user_id: Mapped[int | None] = mapped_column(BigInteger)  # 부서장 (users FK는 순환 회피)


class OrgSettings(Base):
    """기관 설정 (마이그 0162) — 공식 문서(증명서·계약서·급여명세) 발급기관 표기의 SoT.

    단일행(id=1 고정) — 관리화면(/admin/org-settings)에서 ADMIN이 설정. 우선순위:
    DB(name) > env ORG_NAME > 폴백("본 의료기관"). ⚠ hospital_code(env)는 HIS/Clinic 연동
    헤더용 식별자라 별개 — 여기 care_org_no는 문서 표기용(요양기관기호).
    """
    __tablename__ = "org_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_org_settings_singleton"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=1)
    name: Mapped[str] = mapped_column(String(120))                    # 기관 정식명칭
    representative: Mapped[str | None] = mapped_column(String(80))    # 대표자(병원장)
    biz_reg_no: Mapped[str | None] = mapped_column(String(20))        # 사업자등록번호
    care_org_no: Mapped[str | None] = mapped_column(String(20))       # 요양기관기호(문서 표기)
    address: Mapped[str | None] = mapped_column(String(200))
    tel: Mapped[str | None] = mapped_column(String(30))
    domain: Mapped[str | None] = mapped_column(String(200))           # 기관 도메인 주소
    # 회계연도 시작월(1~12·기본 1=역년) — FY N = N년 시작월 1일 ~ +12개월−1일. 재무제표·결산·
    # 공시·이연법인세에 적용. 세무(부가세·세금계산서)·연말정산·4대보험은 법정 달력연도 유지.
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, default=1)
    extra: Mapped[list | None] = mapped_column(JSON)                  # 기타 커스텀 항목 [{key,value}]
    # Phase0 가명화 컷오버 런타임 플래그(0242) — ON이면 inbound consumer가 patientRef 역해소.
    # env HIS_PSEUDONYM_ENABLED와 OR(둘 중 하나 ON=활성). UI 토글로 무재시작 컷오버·즉시 롤백.
    phase0_cutover_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 기관 단위 메뉴 비활성(그룹웨어 전자결재 등 외부 시스템과 중복되는 화면 숨김·마이그 0245).
    # 역할 무관 전역 숨김 — MenuPermission(menu_key,role)으로는 역할 7개를 전부 꺼야 해
    # 하나만 놓쳐도 노출되던 문제. **메뉴 노출만** 제어하고 EP·데이터는 건드리지 않는다.
    disabled_menu_keys: Mapped[list | None] = mapped_column(JSON)
    # HIS SSO 정책(0248) — {"token_mode": "allow"|"block", "enforce_audience": bool}.
    # NULL/키 부재 = 미결정 → 종전 동작(token 허용·aud는 env만). 관리화면에서 결정.
    sso_policy: Mapped[dict | None] = mapped_column(JSON)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('DIRECTOR','ADMIN','MANAGER','STAFF',"
                        "'IT_MANAGER','FINANCE_MANAGER','LOGISTICS_MANAGER')", name="ck_users_role"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))  # argon2id
    role: Mapped[str] = mapped_column(String(32), default="STAFF")  # 원장/관리자/부서장/직원 (LOGISTICS_MANAGER=17자)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("common.departments.id"))
    employee_id: Mapped[int | None] = mapped_column(BigInteger)  # hr.employees (서비스 경유 조회)
    license_flag_active: Mapped[bool] = mapped_column(Boolean, default=True)  # FR-47
    token_version: Mapped[int] = mapped_column(Integer, default=0)  # 강제 무효화 (design §6.1)
    pin_hash: Mapped[str | None] = mapped_column(String(255))   # PIN(argon2) — PIN+패스키 2FA
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefreshToken(Base):
    """refresh 회전·재사용 탐지 저장소 — design §3.5/§6.1 (원자 CAS는 서비스에서)."""
    __tablename__ = "refresh_tokens"
    __table_args__ = {"schema": "common"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("common.users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # SHA-256, 원문 미저장
    family_id: Mapped[str] = mapped_column(String(36), index=True)    # 회전 체인
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[str | None] = mapped_column(String(64))  # 후속 토큰 hash


class Notification(Base):
    """In-app 알림 센터 — FR-26."""
    __tablename__ = "notifications"
    __table_args__ = {"schema": "common"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("common.users.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))  # LICENSE_EXPIRY | STOCK | APPROVAL | APPEAL_DEADLINE ...
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    href: Mapped[str | None] = mapped_column(String(200))  # 딥링크(프론트 라우트·X2-3 마이그0190)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Attachment(Base):
    """증빙 문서 첨부·보관 (범용) — entity_type/entity_id로 전표·세금계산서·입고 등에 연결.

    파일 본문은 디스크(attachment_dir)에 sha256 키로 저장(중복제거), DB는 메타데이터만.
    """
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('tax_invoice','voucher','po_receipt','contract','employee','other',"
            "'it_asset','itam_contract','software_service','itam_maintenance','itam_offboarding',"
            "'org','expense','draft')",
            name="ck_attachment_entity"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(20), index=True)
    entity_id: Mapped[int] = mapped_column(BigInteger, index=True)
    filename: Mapped[str] = mapped_column(String(255))          # 원본 표시명(다운로드용)
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)  # 저장 키 = 내용 해시(중복제거)
    uploaded_by: Mapped[int | None] = mapped_column(BigInteger)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContractAmendment(Base):
    """계약 변경 이력(공통) — 위수탁·의료 계약 공유. kind로 다형 식별(스키마 교차 FK 회피).

    change_type: AMOUNT(금액)·PERIOD(기간)·RENEW(갱신). contract_kind: MEDICAL|OUTSOURCING.
    """
    __tablename__ = "contract_amendments"
    __table_args__ = (
        CheckConstraint("contract_kind IN ('MEDICAL','OUTSOURCING')", name="ck_camend_kind"),
        CheckConstraint("change_type IN ('AMOUNT','PERIOD','RENEW')", name="ck_camend_type"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    contract_kind: Mapped[str] = mapped_column(String(12))   # MEDICAL|OUTSOURCING
    contract_id: Mapped[int] = mapped_column(BigInteger)
    change_type: Mapped[str] = mapped_column(String(10))     # AMOUNT|PERIOD|RENEW
    old_value: Mapped[str | None] = mapped_column(String(60))
    new_value: Mapped[str | None] = mapped_column(String(60))
    effective_on: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalRule(Base):
    """N단계 결재선 룰 — 문서유형·분류·금액대별 결재 단계 정의(공통 엔진).

    steps: 순서 배열 [{type: ROLE|DEPT_HEAD|USER, value: 역할명|user_id|null}, ...].
    매칭: doc_type + (category 일치 or null) + min_amount<=금액<(max_amount or ∞), category 우선.
    """
    __tablename__ = "approval_rules"
    __table_args__ = {"schema": "common"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    doc_type: Mapped[str] = mapped_column(String(24), index=True)   # PO|OUTSOURCING_CONTRACT|MEDICAL_CONTRACT
    category: Mapped[str | None] = mapped_column(String(24))        # null=전체
    min_amount: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    max_amount: Mapped[float | None] = mapped_column(Numeric(16, 0))  # null=상한 없음
    steps: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalInstance(Base):
    """결재 인스턴스 — 문서 1건의 진행 중 결재선(단계는 approval_steps)."""
    __tablename__ = "approval_instances"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','CANCELED')",
                        name="ck_appinst_status"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    doc_type: Mapped[str] = mapped_column(String(24), index=True)
    doc_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[float] = mapped_column(Numeric(16, 0), default=0)
    department_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10), default="PENDING")
    current_step: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalStep(Base):
    """결재 단계 — 인스턴스의 순차 단계(승인자 유형·결정·이력)."""
    __tablename__ = "approval_steps"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','SKIPPED')",
                        name="ck_appstep_status"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("common.approval_instances.id"), index=True)
    step_no: Mapped[int] = mapped_column(Integer)
    approver_type: Mapped[str] = mapped_column(String(12))   # ROLE|DEPT_HEAD|USER
    approver_value: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(10), default="PENDING")
    acted_by: Mapped[int | None] = mapped_column(BigInteger)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(300))


class ApprovalLedger(Base):
    """결재 불변 원장(블록체인식 해시체인) — 결재 행위(상신·승인·반려·전권)의 변조 증거 기록.

    append-only: row_hash = SHA-256(prev_hash ‖ 결정 필드) 전역 단조 체인(seq_no). DB 계층에서
    erp_app UPDATE/DELETE REVOKE(마이그 0203 — narcotics_ledger·voucher_lines 동일 패턴).
    검증(verify)이 전 구간을 재계산 대조 → superuser 변조까지 사후 탐지. 감사로그(AuditService)와
    별개 계층: 감사=조회·맥락, 본 원장=암호학적 무결성.
    """
    __tablename__ = "approval_ledger"
    __table_args__ = (
        UniqueConstraint("seq_no", name="uq_approval_ledger_seq"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    seq_no: Mapped[int] = mapped_column(BigInteger)                  # 전역 단조(체인 순서)
    instance_id: Mapped[int] = mapped_column(BigInteger, index=True)
    doc_type: Mapped[str] = mapped_column(String(24), index=True)
    doc_id: Mapped[int] = mapped_column(BigInteger)
    step_no: Mapped[int | None] = mapped_column(Integer)             # BUILD는 None
    action: Mapped[str] = mapped_column(String(10))                  # BUILD|APPROVE|REJECT|OVERRIDE
    actor: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(300))
    acted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 행위 시각(해시 입력)
    prev_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalLedgerAnchor(Base):
    """결재 원장 앵커(체크포인트) — 검증된 체인 head를 봉인해 꼬리 절단(최근 행 삭제) 탐지.

    주기 검증 잡이 verify 통과 시 (seq_no, head_hash, entry_count)를 append하고 MAC 서명.
    다음 검증은 "현재 체인이 이 앵커 지점까지 동일 head를 재현하고 그 뒤로만 확장됐는지" 대조 →
    앵커 이후 최근 행을 지워 길이를 줄이면 entry_count 미달로 적발(해시체인만으론 못 잡는 사각).
    append-only(트리거·마이그 0204)·mac은 원장과 같은 서명키.
    """
    __tablename__ = "approval_ledger_anchor"
    __table_args__ = {"schema": "common"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    seq_no: Mapped[int] = mapped_column(BigInteger)          # 앵커 시점 체인 마지막 seq
    entry_count: Mapped[int] = mapped_column(BigInteger)     # 앵커 시점 총 엔트리 수
    head_hash: Mapped[str] = mapped_column(String(64))       # seq_no 위치 running MAC
    mac: Mapped[str] = mapped_column(String(64))             # HMAC(key, seq|count|head)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MenuPermission(Base):
    """역할별 메뉴 노출 오버라이드 — 코드 카탈로그 기본값 위에 (menu_key,role) 단위 재정의."""
    __tablename__ = "menu_permissions"
    __table_args__ = (
        UniqueConstraint("menu_key", "role", name="uq_menu_perm"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    menu_key: Mapped[str] = mapped_column(String(64), index=True)   # = href
    role: Mapped[str] = mapped_column(String(32))   # 역할명 저장 (LOGISTICS_MANAGER=17자)
    visible: Mapped[bool] = mapped_column(Boolean)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class UserCredential(Base):
    """WebAuthn 패스키(스마트폰 생체) 자격증명 — 사용자당 N개. 본문키는 base64url 저장."""
    __tablename__ = "user_credentials"
    __table_args__ = (
        UniqueConstraint("credential_id", name="uq_user_credential"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("common.users.id"), index=True)
    credential_id: Mapped[str] = mapped_column(String(255))     # base64url
    public_key: Mapped[str] = mapped_column(Text)              # base64url(COSE)
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0)
    transports: Mapped[str | None] = mapped_column(String(120))
    name: Mapped[str | None] = mapped_column(String(80))        # 기기 별칭
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkstationPass(Base):
    """재고 워크스테이션 일일 출입증(알바용) — 당일 PIN. WORKSTATION 제한 세션만 발급."""
    __tablename__ = "workstation_passes"
    __table_args__ = {"schema": "common"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    pin_hash: Mapped[str] = mapped_column(String(255))      # argon2
    label: Mapped[str | None] = mapped_column(String(80))   # 용도/담당(예: 김알바 오전)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 당일 종료시각
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Holiday(Base):
    """공휴일 캘린더 — 변동 공휴일(음력 명절·대체공휴일)을 코드 하드코딩 대신 데이터로 관리(P3).

    고정 양력 공휴일·주말은 코드(claims/deadline.FIXED_HOLIDAYS)로 처리, 본 테이블은 매년
    바뀌는 음력·대체공휴일을 담아 법정 기한(§161)·근태 계산에서 참조. 관리자 유지(연 1회 갱신).
    """
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("on_date", name="uq_holiday_date"), {"schema": "common"})

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    on_date: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(16), default="PUBLIC")   # PUBLIC|SUBSTITUTE|LUNAR|TEMP
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HolidayCandidateReview(Base):
    """공휴일 후보 검토 기록 (0264) — 해마다 바뀌는 공휴일을 코드 후보(`common.holiday_candidates`)와 원문 대조해 확정한 이력.

    2026-10-11: 노동절·제헌절이 2026년 관공서 공휴일로 지정됐는데 코드 고정 공휴일·DB 어디에도 없었다. 법정 날짜는 사람이 원문
    (월력요항·관공서의 공휴일에 관한 규정)과 대조해 확정한다 — 등록한 날짜와 뺀 날짜(이견 등)를 근거와 함께 남기면 그 해 항목이 닫힌다.
    """
    __tablename__ = "holiday_candidate_reviews"
    __table_args__ = {"schema": "common"}

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    registered: Mapped[list] = mapped_column(JSON)          # 등록한 날짜(ISO)
    excluded: Mapped[list] = mapped_column(JSON)            # 뺀 날짜(ISO) — 이견·해당 없음
    source_note: Mapped[str] = mapped_column(String(500))   # 대조 근거(필수)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 결재선 요청·관리 (기안문서 선행 기반·마이그 0212) ─────────────────────
class ApprovalLineTemplate(Base):
    """결재선 템플릿 — 상신 시 골라 쓰는 명명 결재선. ORG(공용·요청 승인으로 프로비저닝)/PERSONAL(개인).

    steps = [{type: ROLE|DEPT_HEAD|USER, value}] (엔진 step 규약 재사용). doc_types = 적용 문서유형 배열
    또는 ["ANY"]. build 시 steps를 인스턴스로 복사(스냅샷 — 이후 템플릿 변경이 진행 중 문서에 소급 안 됨).
    """
    __tablename__ = "approval_line_templates"
    __table_args__ = (
        CheckConstraint("scope IN ('ORG','PERSONAL')", name="ck_apl_scope"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    scope: Mapped[str] = mapped_column(String(10), default="PERSONAL", index=True)
    owner_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # PERSONAL 소유자
    department_id: Mapped[int | None] = mapped_column(BigInteger)              # ORG 귀속(선택)
    doc_types: Mapped[list] = mapped_column(JSON, default=list)                # ["GENERAL_DRAFT"]|["ANY"]
    steps: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source_request_id: Mapped[int | None] = mapped_column(BigInteger)          # 프로비저닝 출처
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalLineRequest(Base):
    """결재선 요청 — 표준(ORG) 결재선 신설/변경/폐기 요청 → 관리자 검토 → 승인 시 템플릿 프로비저닝.

    R-4(결정): 요청 자체는 경량 승인(관리자 단일 결정·순환의존 회피). 결정은 감사로그 기록(FR-6).
    """
    __tablename__ = "approval_line_requests"
    __table_args__ = (
        CheckConstraint("request_type IN ('NEW','MODIFY','RETIRE')", name="ck_aplr_type"),
        CheckConstraint("status IN ('REQUESTED','APPROVED','REJECTED','CANCELED')",
                        name="ck_aplr_status"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    req_no: Mapped[str] = mapped_column(String(24), unique=True)               # 결재선요청-YYYY-NNNN
    request_type: Mapped[str] = mapped_column(String(10), default="NEW")
    target_template_id: Mapped[int | None] = mapped_column(BigInteger)         # MODIFY/RETIRE 대상
    proposed_name: Mapped[str | None] = mapped_column(String(120))
    proposed_steps: Mapped[list] = mapped_column(JSON, default=list)
    doc_types: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str | None] = mapped_column(String(400))
    department_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10), default="REQUESTED", index=True)
    requested_by: Mapped[int] = mapped_column(BigInteger)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 자금결재 기안문서 — 자금 관련 품의·공람(ERP 내부 결재·그룹웨어 전자결재와 별개·마이그 0213) ──
class GeneralDraft(Base):
    """기안문서 — 자유형식 품의/업무협조. 결재선은 상신 시 템플릿 선택(R-2)·엔진 인스턴스 재사용.

    회계·전표 없음(품의≠집행). ref_amount는 참고용. doc_no는 상신 시 채번(결정 E).
    """
    __tablename__ = "general_drafts"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','PENDING','APPROVED','REJECTED','CANCELED')",
                        name="ck_gdraft_status"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    doc_no: Mapped[str | None] = mapped_column(String(24), unique=True)   # 기안-YYYY-NNNN(상신 시)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(24), default="GENERAL")  # GENERAL|BUDGET|POLICY|COOPERATION
    department_id: Mapped[int | None] = mapped_column(BigInteger)
    ref_amount: Mapped[float | None] = mapped_column(Numeric(16, 0))      # 참고 금액(집행 아님)
    status: Mapped[str] = mapped_column(String(10), default="DRAFT", index=True)
    created_by: Mapped[int] = mapped_column(BigInteger, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DraftViewer(Base):
    """기안문서 공람(CC) — 결재 완료 시 알림 + 열람 기록."""
    __tablename__ = "draft_viewers"
    __table_args__ = (
        UniqueConstraint("draft_id", "user_id", name="uq_draft_viewer"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("common.general_drafts.id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PendingDecision(Base):
    """결정 대기함 (0226) — 감사/기획의 '사람 결정' 승격 항목을 관리 기능화.

    선택지(options JSON)·권고안 제시 → 관리자 결정 기록(감사추적) → 후속 구현의 근거.
    key 유니크(시드 멱등)·status: PENDING→DECIDED / DEFERRED(보류)→재오픈.
    """
    __tablename__ = "pending_decisions"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','DECIDED','DEFERRED')", name="ck_pdec_status"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    category: Mapped[str] = mapped_column(String(20), default="GENERAL")  # PRIVACY|ARCH|INFRA|…
    title: Mapped[str] = mapped_column(String(200))
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON)     # [{key,label,description,recommended}]
    context_ref: Mapped[str | None] = mapped_column(String(300))   # 근거 문서 경로
    status: Mapped[str] = mapped_column(String(10), default="PENDING", index=True)
    decided_option: Mapped[str | None] = mapped_column(String(40))
    decided_note: Mapped[str | None] = mapped_column(String(1000))
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(BigInteger)     # NULL=시스템(감사) 등록
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecurringTask(Base):
    """정기업무 정의(주/월/분기 고정·반복업무 레지스트리) — 러너=erp.app.recurring_service.

    execution_mode: AUTO(멱등 자동)·AI_ASSISTED(AI 초안→사람 확정)·MANUAL(체크리스트). handler_ref=
    common.recurring 핸들러 등록키(AUTO/AI_ASSISTED만·MANUAL은 null). anchor={basis,offset_days,
    hour,minute} — PERIOD_END·offset로 '분기 종료 후 익월 25일(부가세)' 등 표현. requires_approval=
    True는 자동실행 금지(자금·의료법 게이트). 기획서 erp-recurring-operations-plan-2026-07-25.md.
    """
    __tablename__ = "recurring_tasks"
    __table_args__ = (
        CheckConstraint("cadence IN ('DAILY','WEEKLY','MONTHLY','QUARTERLY','YEARLY')",
                        name="ck_rectask_cadence"),
        CheckConstraint("execution_mode IN ('AUTO','AI_ASSISTED','MANUAL')",
                        name="ck_rectask_mode"),
        CheckConstraint("category IN ('FIXED','RECURRING')", name="ck_rectask_category"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    domain: Mapped[str] = mapped_column(String(20))                # finance|hr|scm|regulatory|…
    category: Mapped[str] = mapped_column(String(10), default="RECURRING")   # FIXED|RECURRING
    cadence: Mapped[str] = mapped_column(String(10))
    anchor: Mapped[dict] = mapped_column(JSON, default=dict)       # {basis,offset_days,hour,minute}
    execution_mode: Mapped[str] = mapped_column(String(12), default="MANUAL")
    handler_ref: Mapped[str | None] = mapped_column(String(64))    # 핸들러 등록키(AUTO/AI_ASSISTED)
    owner_role: Mapped[str | None] = mapped_column(String(30))     # 책임 역할(알림·완료권한)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    sla_days: Mapped[int] = mapped_column(Integer, default=7)      # due+sla<now → OVERDUE
    source_ref: Mapped[str | None] = mapped_column(String(120))    # 컴플라이언스 등 외부 소스 연결
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)    # 소규모 테이블 — 인덱스 불요
    note: Mapped[str | None] = mapped_column(String(400))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecurringTaskRun(Base):
    """정기업무 실행이력 — UNIQUE(task_code,period_key)=기간당 1회(멱등·claim 락 역할).

    상태: DUE(물질화)→RUNNING(claim)→DONE/FAILED, WAITING_APPROVAL(AI_ASSISTED 초안 대기),
    SKIPPED. error=str(exc)[:120](Phase0 불변식)·result JSON은 숫자/집계만(PII·chart_no 금지).
    """
    __tablename__ = "recurring_task_runs"
    __table_args__ = (
        UniqueConstraint("task_code", "period_key", name="uq_rectask_run_period"),
        CheckConstraint(
            "status IN ('DUE','RUNNING','DONE','FAILED','SKIPPED','WAITING_APPROVAL')",
            name="ck_rectask_run_status"),
        {"schema": "common"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # task_code 조회는 uq_rectask_run_period(task_code 선두)로 커버 — 별도 인덱스 불요
    task_code: Mapped[str] = mapped_column(String(64))
    period_key: Mapped[str] = mapped_column(String(20))           # 2026-W30|2026-07|2026-Q3|2026
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # KST 벽시계 naive
    status: Mapped[str] = mapped_column(String(16), default="DUE")
    triggered_by: Mapped[str | None] = mapped_column(String(12))  # SCHEDULER|CATCHUP|ADMIN_UI|AI_CLI
    actor: Mapped[int | None] = mapped_column(BigInteger)         # 실행 주체(자동=0/SYSTEM)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)             # 처리 건수·요약(숫자만)
    error: Mapped[str | None] = mapped_column(String(120))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    decision_key: Mapped[str | None] = mapped_column(String(64))  # AI_ASSISTED 승인 연결(PendingDecision.key)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
