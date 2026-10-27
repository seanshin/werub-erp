"""claims 스키마 — design §3.3. 매핑 파서는 G1 Gate (실물 샘플 확보 후)."""
from datetime import date, datetime

from sqlalchemy import (
    JSON, BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp.db import Base, BigIntPK


class NoticeFileStaging(Base):
    """통보서 수신 스테이징 (integration 스키마) — raw 보존·실패 격리 (design §5.2)."""
    __tablename__ = "hira_notice_staging"
    __table_args__ = (
        CheckConstraint("status IN ('NEW','OK','FAILED','REPROCESSED')", name="ck_staging_status"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    stored_name: Mapped[str] = mapped_column(String(64), unique=True)  # 서버 생성 UUID (§6.7)
    original_name: Mapped[str] = mapped_column(String(200))            # 메타로만 보관
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    uploaded_by: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(12), default="NEW")
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClaimBatch(Base):
    """통보서 접수번호 단위."""
    __tablename__ = "claim_batches"
    __table_args__ = (
        CheckConstraint("status IN ('DECIDED','PARTIALLY_PAID','PAID')",
                        name="ck_claim_batch_status"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    receipt_no: Mapped[str] = mapped_column(String(40), unique=True)
    care_month: Mapped[str] = mapped_column(String(7))
    decided_on: Mapped[date | None] = mapped_column(Date)
    notice_arrived_on: Mapped[date] = mapped_column(Date)   # 도달일 — 기한 기산 (FR-40)
    claimed_amt: Mapped[float | None] = mapped_column(Numeric(14, 0))
    decided_amt: Mapped[float | None] = mapped_column(Numeric(14, 0))
    # 지급 생애주기(마이그 0156) — 심사완료→(부분)지급→지급완료. 지급 대사(0155) 연동 갱신.
    status: Mapped[str] = mapped_column(String(16), default="DECIDED", index=True)
    staging_id: Mapped[int | None] = mapped_column(BigInteger)  # 출처 스테이징


class ClaimStatement(Base):
    """명세서(명일련번호) 단위."""
    __tablename__ = "claim_statements"
    __table_args__ = (
        UniqueConstraint("batch_id", "stmt_serial", name="uq_stmt"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("claims.claim_batches.id"), index=True)
    stmt_serial: Mapped[str] = mapped_column(String(10))
    chart_no: Mapped[str | None] = mapped_column(String(20))  # patient 조인 금지 — 서비스 조회
    dept_code: Mapped[str | None] = mapped_column(String(10))
    doctor_id: Mapped[int | None] = mapped_column(BigInteger)
    claimed_amt: Mapped[float | None] = mapped_column(Numeric(14, 0))
    adjusted_amt: Mapped[float | None] = mapped_column(Numeric(14, 0))
    decided_amt: Mapped[float | None] = mapped_column(Numeric(14, 0))


class ClaimAdjustment(Base):
    """조정(삭감) 내역 — 자동화율 분모/분자 = map_status (Plan §4.3)."""
    __tablename__ = "claim_adjustments"
    __table_args__ = (
        CheckConstraint("map_status IN ('AUTO','MANUAL','UNMAPPED')", name="ck_adj_map"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("claims.claim_statements.id"), index=True)
    adj_code: Mapped[str | None] = mapped_column(String(10))   # G1: 코드 마스터는 M0c 후
    adj_category: Mapped[str | None] = mapped_column(String(20))
    adj_amt: Mapped[float] = mapped_column(Numeric(14, 0))
    adj_reason: Mapped[str | None] = mapped_column(Text)
    map_status: Mapped[str] = mapped_column(String(10), default="UNMAPPED")


class Appeal(Base):
    """이의신청 (FR-40) — 법정 기한 90일 (G5: 역일 가정)."""
    __tablename__ = "appeals"
    __table_args__ = (
        CheckConstraint("status IN ('RECEIVED','SUPPLEMENT','REJECTED','ACCEPTED')",
                        name="ck_appeal_status"),
        CheckConstraint("appeal_type IN ('REVIEW_REQUEST','OBJECTION')", name="ck_appeal_type"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    adjustment_id: Mapped[int] = mapped_column(ForeignKey("claims.claim_adjustments.id"),
                                               index=True)
    appeal_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12), default="RECEIVED")
    notice_arrived_on: Mapped[date] = mapped_column(Date)
    legal_deadline: Mapped[date] = mapped_column(Date, index=True)  # 도달일+90 (G5 역일 가정)
    final_loss_amt: Mapped[float | None] = mapped_column(Numeric(14, 0))
    voucher_ref: Mapped[str | None] = mapped_column(String(64))
    # 기한임박 알림 마지막 발송 임계(가장 작은 D-버킷) — 무손실·멱등 발송(마이그 0154)
    alert_stage: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClaimPayment(Base):
    """공단 지급 대사 (마이그 0155) — 보험미수금(1140) 생애주기 완결.

    심사청구 회계가 발생(claim.submitted)·삭감(claim.reviewed)까지만 있어 공단 실지급이 AR을
    소멸시키지 못하던 갭 해소. 지급명세(payment_ref) 단위 입금 대사 + DR 1110/CR 1140 전기,
    결정액(expected) 대비 variance(과소지급) 산출.
    """
    __tablename__ = "claim_payments"
    __table_args__ = (
        UniqueConstraint("payment_ref", name="uq_claim_payment_ref"),   # 멱등 사업키
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    payment_ref: Mapped[str] = mapped_column(String(60))
    care_month: Mapped[str] = mapped_column(String(7), index=True)
    paid_on: Mapped[date] = mapped_column(Date)
    paid_amt: Mapped[float] = mapped_column(Numeric(14, 0))
    expected_amt: Mapped[float | None] = mapped_column(Numeric(14, 0))   # 결정액 스냅샷
    variance: Mapped[float | None] = mapped_column(Numeric(14, 0))       # expected-paid(양수=과소지급)
    note: Mapped[str | None] = mapped_column(Text)
    voucher_ref: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClaimResubmission(Base):
    """반송(반려) 청구 재청구 추적 (마이그 0157) — 이의신청(사후 삭감분쟁)과 별개.

    심사 전 반송된 청구의 정정·재제출을 추적. 반송 후 미재청구 청구를 놓쳐 수익 유실하는 것 방지
    (RETURNED=미재청구). 운영 추적 — 회계 전기 없음(최종 결정은 기존 claim.reviewed 경로).
    """
    __tablename__ = "claim_resubmissions"
    __table_args__ = (
        UniqueConstraint("original_ref", name="uq_claim_resubmission_ref"),
        CheckConstraint("status IN ('RETURNED','RESUBMITTED','CLOSED')",
                        name="ck_claim_resubmission_status"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    original_ref: Mapped[str] = mapped_column(String(40))   # 원 청구 식별(멱등 사업키)
    care_month: Mapped[str] = mapped_column(String(7), index=True)
    returned_on: Mapped[date] = mapped_column(Date)
    return_code: Mapped[str | None] = mapped_column(String(20))
    return_reason: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[float | None] = mapped_column(Numeric(14, 0))
    resubmit_ref: Mapped[str | None] = mapped_column(String(40))   # 재청구 접수번호
    resubmitted_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="RETURNED", index=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClaimSubmission(Base):
    """제출 청구 캡처 (마이그 0158) — 청구액 이중소스 정합.

    claim.submitted(HIS webhook)가 AR(1140)만 전기하고 청구를 기록하지 않아 MEDRLT 청구액과 대사
    불가하던 갭. 제출 청구를 claim_id 멱등 캡처 → 진료월 제출액 vs 심사대상 청구액 불일치 탐지.
    care_month 미동봉 시 NULL(미대사로 표면화).
    """
    __tablename__ = "claim_submissions"
    __table_args__ = (
        UniqueConstraint("claim_id", name="uq_claim_submission_id"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    claim_id: Mapped[str] = mapped_column(String(64))
    claim_number: Mapped[str | None] = mapped_column(String(64))
    care_month: Mapped[str | None] = mapped_column(String(7), index=True)
    claimed_amt: Mapped[float] = mapped_column(Numeric(14, 0))
    submitted_on: Mapped[date | None] = mapped_column(Date)
    # 결정 마커(마이그 0159) — claim.reviewed가 마킹. 변동대가 미결정 풀 as-of 재구성 기준.
    decided_on: Mapped[date | None] = mapped_column(Date)
    decided_amt: Mapped[float | None] = mapped_column(Numeric(14, 0))
    decided_rev: Mapped[int | None] = mapped_column(Integer)   # 반영한 심사 정정 버전(0252)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PreReviewRule(Base):
    """사전심사 룰 (FR-41) — 청구 전 삭감 예방 규칙. params는 룰타입별 JSON 설정."""
    __tablename__ = "pre_review_rules"
    __table_args__ = (
        CheckConstraint(
            "rule_type IN ('DUPLICATE','COUNT_LIMIT','UNKNOWN_CODE','AMOUNT_OUTLIER',"
            "'AGE_SEX_LIMIT','EXCLUSIVE_PAIR','PERIOD_LIMIT','SURCHARGE_CHECK',"
            "'LOS_RATIO','STAT_OUTLIER','NONCOVERED_CLAIM','ELIGIBILITY_INVALID')",
            name="ck_prerule_type"),
        CheckConstraint("severity IN ('WARN','BLOCK')", name="ck_prerule_sev"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    rule_type: Mapped[str] = mapped_column(String(20))
    params: Mapped[dict | None] = mapped_column(JSON)
    severity: Mapped[str] = mapped_column(String(8), default="WARN")
    active: Mapped[bool] = mapped_column(default=True)
    valid_from: Mapped[date | None] = mapped_column(Date)   # 고시 효력일(NULL=상시) — 시점 유효룰(G7)
    valid_to: Mapped[date | None] = mapped_column(Date)     # 폐기일(NULL=현행)
    # 의학판단 필요 룰 — 심사자 단독 오탐 금지, 의료진 질의·확인 경유로만 종결(의료법: 비의료인 의학판단 금지)
    physician_gated: Mapped[bool] = mapped_column(default=False)


class PreReviewCase(Base):
    """심사 워크리스트 케이스 — 차트(chart_no)×진료월 단위, 재실행 멱등 upsert (PR-A)."""
    __tablename__ = "pre_review_cases"
    __table_args__ = (
        UniqueConstraint("chart_no", "care_month", name="uq_prereview_case"),
        CheckConstraint("status IN ('OPEN','IN_REVIEW','CLEARED','FIX_REQUESTED',"
                        "'BLOCKED','RESOLVED')", name="ck_prc_status"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    chart_no: Mapped[str] = mapped_column(String(20))
    care_month: Mapped[str] = mapped_column(String(7))      # YYYY-MM
    dept_code: Mapped[str | None] = mapped_column(String(10))
    encounter_type: Mapped[str | None] = mapped_column(String(12))  # INPATIENT|OUTPATIENT|MIXED
    status: Mapped[str] = mapped_column(String(14), default="OPEN", index=True)
    max_severity: Mapped[str | None] = mapped_column(String(8))
    risk_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    claimed_amt: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    finding_count: Mapped[int] = mapped_column(Integer, default=0)
    assignee_id: Mapped[int | None] = mapped_column(BigInteger)
    auto_cleared: Mapped[bool] = mapped_column(default=False)  # P1 자동 이상없음(무적발) 표시 — 가역
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PreReviewFinding(Base):
    """케이스 적발 항목 — 결정(인정/오탐=캘리브레이션 데이터)·재실행 해소 추적."""
    __tablename__ = "pre_review_findings"
    __table_args__ = (
        CheckConstraint("decision IN ('PENDING','ACCEPT','DISMISS')", name="ck_prf_decision"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("claims.pre_review_cases.id"), index=True)
    rule_code: Mapped[str] = mapped_column(String(30))
    rule_name: Mapped[str | None] = mapped_column(String(120))
    severity: Mapped[str] = mapped_column(String(8))
    reason_group: Mapped[str | None] = mapped_column(String(2))   # 거부사유 대분류(A/B/D/K/M)
    reason_code: Mapped[str | None] = mapped_column(String(12))   # 거부사유 세분(예: B-CNT)
    code: Mapped[str | None] = mapped_column(String(20))
    treated_on: Mapped[date | None] = mapped_column(Date)   # 진료일(날짜특정 적발 — 명세 라인 정확 부착)
    detail: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    decision: Mapped[str] = mapped_column(String(8), default="PENDING")
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_decided: Mapped[bool] = mapped_column(default=False)  # P2 자동 오탐(캘리브레이션) — 인간결정과 구분·집계제외
    resolved: Mapped[bool] = mapped_column(default=False)   # 재실행에서 미재현 = 해소
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PreReviewComment(Base):
    """케이스 검토 코멘트 스레드."""
    __tablename__ = "pre_review_comments"
    __table_args__ = {"schema": "claims"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("claims.pre_review_cases.id"), index=True)
    author: Mapped[int] = mapped_column(BigInteger)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PreReviewQuery(Base):
    """의료진 질의 (P2) — 심사팀이 적발/라인에 대해 의료진에게 근거·수정 요청.

    삭감 방지 루프: 위험 식별 → 질의 → 의료진 회신(근거|수정) → 적발 해소/DISMISS.
    대상은 적발(finding_id)·라인(line_code) 둘 다 가능(적발 없는 라인도 질의).
    """
    __tablename__ = "pre_review_queries"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','ANSWERED','ACCEPTED','REVISED')",
                        name="ck_prq_status"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("claims.pre_review_cases.id"),
                                         index=True)
    finding_id: Mapped[int | None] = mapped_column(
        ForeignKey("claims.pre_review_findings.id"))      # 적발 연결(선택)
    line_code: Mapped[str | None] = mapped_column(String(20))   # 라인 코드(선택)
    status: Mapped[str] = mapped_column(String(10), default="OPEN", index=True)
    question: Mapped[str] = mapped_column(Text)
    raised_by: Mapped[int] = mapped_column(BigInteger)         # 심사팀 user
    assigned_to: Mapped[int | None] = mapped_column(BigInteger)  # 의료진 employee_id
    confirmed_by_employee: Mapped[int | None] = mapped_column(BigInteger)  # 확인 책임 의료진(종결 시)
    resolution_note: Mapped[str | None] = mapped_column(Text)  # 종결 사유
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PreReviewReply(Base):
    """의료진 회신 (P2) — 근거 제시(EVIDENCE) 또는 오더 수정 통보(REVISE)."""
    __tablename__ = "pre_review_replies"
    __table_args__ = (
        CheckConstraint("reply_type IN ('EVIDENCE','REVISE')", name="ck_prr_type"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    query_id: Mapped[int] = mapped_column(ForeignKey("claims.pre_review_queries.id"),
                                          index=True)
    reply_type: Mapped[str] = mapped_column(String(10))
    body: Mapped[str] = mapped_column(Text)
    author: Mapped[int] = mapped_column(BigInteger)            # 의료진/심사팀 user
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class PreReviewLearningCase(Base):
    """사전심사 학습 사례 — 결정된 적발(인정=삭감위험/오탐=정상)을 라벨된 AI 학습 데이터로 등록·관리.

    자동 수확(human 결정 적발) + 수동 등록. 품질 승인·의료진 확인된 건만 학습 적격(eligible)으로
    내보내기(JSONL). label=REDUCTION(삭감위험)|FALSE_POSITIVE(오탐). features=학습 피처 스냅샷.
    """
    __tablename__ = "pre_review_learning_cases"
    __table_args__ = (
        UniqueConstraint("source_finding_id", name="uq_prlc_finding"),  # 자동수확 멱등(NULL 다중 허용)
        CheckConstraint("label IN ('REDUCTION','FALSE_POSITIVE')", name="ck_prlc_label"),
        CheckConstraint("quality IN ('DRAFT','APPROVED','REJECTED')", name="ck_prlc_quality"),
        CheckConstraint("source IN ('AUTO','MANUAL')", name="ck_prlc_source"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source_finding_id: Mapped[int | None] = mapped_column(BigInteger)   # 원 적발(자동수확)·수동은 NULL
    chart_no: Mapped[str | None] = mapped_column(String(20))
    dept_code: Mapped[str | None] = mapped_column(String(10), index=True)  # 부서경계(IDOR/PII·마이그 0153)
    care_month: Mapped[str | None] = mapped_column(String(7))
    encounter_type: Mapped[str | None] = mapped_column(String(12))
    rule_code: Mapped[str | None] = mapped_column(String(30))
    rule_type: Mapped[str | None] = mapped_column(String(20))
    code: Mapped[str | None] = mapped_column(String(20))         # 적발 수가/약품 코드
    diagnosis_codes: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    label: Mapped[str] = mapped_column(String(16))              # REDUCTION | FALSE_POSITIVE
    rationale: Mapped[str | None] = mapped_column(Text)         # 근거/사유(결정노트·의료진 근거)
    physician_confirmed: Mapped[bool] = mapped_column(default=False)
    confirmed_by_employee: Mapped[int | None] = mapped_column(BigInteger)
    features: Mapped[dict | None] = mapped_column(JSON)         # 학습 피처 스냅샷
    tags: Mapped[str | None] = mapped_column(String(200))
    quality: Mapped[str] = mapped_column(String(10), default="DRAFT")  # DRAFT|APPROVED|REJECTED
    source: Mapped[str] = mapped_column(String(8), default="AUTO")
    active: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), onupdate=func.now())


class DrugIngredientMap(Base):
    """보험 약가코드 → 주성분코드 (DUR 성분 단위 매칭). HIS/약가마스터·식약처 적재."""
    __tablename__ = "drug_ingredient_map"
    __table_args__ = {"schema": "claims"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    insurance_code: Mapped[str] = mapped_column(String(20), unique=True)
    ingredient_code: Mapped[str] = mapped_column(String(20), index=True)
    ingredient_name: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DurRule(Base):
    """DUR 룰 (K-IFRS 무관·의약품안전사용) — 병용금기·동일성분중복·연령금기. 공개데이터 적재."""
    __tablename__ = "dur_rules"
    __table_args__ = (
        CheckConstraint("dur_type IN ('CONTRA','DUP','AGE','PREGNANCY')", name="ck_dur_type"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    dur_type: Mapped[str] = mapped_column(String(10))            # CONTRA|DUP|AGE|PREGNANCY
    ingredient_code: Mapped[str | None] = mapped_column(String(20))
    ingredient_code_b: Mapped[str | None] = mapped_column(String(20))  # 병용금기 쌍 B
    min_age: Mapped[int | None] = mapped_column(Integer)
    max_age: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(300))
    notice_no: Mapped[str | None] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(8), default="BLOCK")
    source: Mapped[str] = mapped_column(String(20), default="MFDS")
    active: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DxProcedureRule(Base):
    """진단(KCD)-수가 연계 룰 (G2) — 수가코드에 필수 상병 prefix. 형식 점검(미기재)."""
    __tablename__ = "dx_procedure_rules"
    __table_args__ = {"schema": "claims"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    require_dx_prefixes: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str | None] = mapped_column(String(120))
    reason: Mapped[str | None] = mapped_column(String(300))
    severity: Mapped[str] = mapped_column(String(8), default="WARN")
    active: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BundlingRule(Base):
    """묶음청구 룰 (G1/G4) — 패키지 수가(parent)에 포함된 세부/재료(child) 별도청구 적발."""
    __tablename__ = "bundling_rules"
    __table_args__ = (
        CheckConstraint("rule_type IN ('BUNDLE','MATERIAL')", name="ck_bundle_type"),
        {"schema": "claims"},
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    parent_code: Mapped[str] = mapped_column(String(20), index=True)
    child_codes: Mapped[str] = mapped_column(String(400))   # 쉼표구분
    rule_type: Mapped[str] = mapped_column(String(10), default="BUNDLE")  # BUNDLE|MATERIAL
    name: Mapped[str | None] = mapped_column(String(120))
    reason: Mapped[str | None] = mapped_column(String(300))
    severity: Mapped[str] = mapped_column(String(8), default="WARN")
    active: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
