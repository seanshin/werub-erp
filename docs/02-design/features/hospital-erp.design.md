# hospital-erp Design Document

> **Summary**: 병원용 ERP 시스템 설계 — Modular Monolith(FastAPI) + 비동기 워커 2종, temporal 수가 마스터, **14 도메인 모듈**, ACL 연계 인터페이스, RBAC/행수준 보안. (Phase 1 8모듈 설계 → 구현 확장은 §12 참조)
>
> **Project**: hospital-erp
> **Version**: 0.5 (구현 동기화 — §12)
> **Author**: weruby
> **Date**: 2026-06-07 (v0.5: 2026-06-15)
> **Status**: 구현 LIVE (프로드 배포, 마이그 head 0061, 테스트 547)
> **Planning Doc**: [hospital-erp.plan.md](../../01-plan/features/hospital-erp.plan.md) (v1.1 Approved)
> **References**: `hospital-erp.prd.md`, `references/hira-review-notice-reference.md`, `references/weve-ai-integration-reply.md`, `references/notice-integration-reply-from-ai.md`

---

## 0. 설계 Gate 현황 (착수 전 필독)

Plan v1.1에서 선언된 설계 Gate — 아래 항목은 **인터페이스·스키마 골격만 본 문서에서 확정**하고, 상세 규칙 설계는 Gate 해소 후 본 문서 개정으로 반영:

| Gate | 대상 | 보류 범위 | 해소 조건 |
|------|------|----------|----------|
| G1 | FR-38 통보서 매핑 | ~~레이아웃 규격~~ → **공고 별표1 확보 (EDIFACT MEDRLT — `references/hira-edi-spec/`)**: 파서 설계·구현 착수 가능. 잔여 보류 = 실물 샘플 검증·조정사유코드 마스터 | M0c 잔여: 실물 샘플 2건(운영 투입 게이트) + HIS 저장 위치 |
| G2 | FR-36 간호등급 | 산식 상세·기준 시점 | M0d: 현행 산식 양식 확보 |
| G3 | FR-32 노동규제 룰 | 룰 테이블 값(조항·기산·임계값) | 인사팀·도메인 전문가 검토 확정 |
| G4 | DB 방언 | PostgreSQL 전제 DDL — **특히 EXCLUDE(GiST) 겹침 방어는 PG 전용**: non-PG면 앱 레벨 직렬화로 전면 재설계 | M0b: 기존 DB 종류·버전 확인 |
| G5 | FR-40 기한 산정 | 이의신청 90일의 법적 성격 (역일 vs 영업일, 공휴일 익영업일 연장 여부) — 설계 단정 금지 | 의료보험팀·법무 확인 (현재는 역일 가정 + 보수적 D-임박 알림) |

---

## 1. Overview

### 1.1 Design Goals

1. **데이터 정합성 최우선** — 수납→전표→삭감 흐름의 단일 트랜잭션, temporal 단가의 과거 시점 100% 재현
2. **모듈 경계의 코드 수준 강제** — 8개 Bounded Context를 스키마·패키지·임포트 규칙으로 분리
3. **외부 의존 격리** — 모든 연계는 ACL 어댑터 + 스테이징 경유, 구현체 교체 가능
4. **HIS 무영향** — 리소스 상한·배치 시간 제약·보안 경계(DB 계정 분리)를 설계 차원에서 보장
5. **검증 가능성** — 모든 비즈니스 규칙은 설계서에 수락 기준 명시 (Gate 항목은 보류 표기)

### 1.2 Design Principles

- Regulatory가 단가·기준의 단일 소스 (타 모듈 복제 금지, 시점 조회만)
- 전표 생성은 Finance 인터페이스 경유만 (직접 INSERT 금지)
- 외부 데이터는 스테이징 → 검증 → 코어 반영 (직접 쓰기 금지)
- 모듈 간 직접 테이블 조인 금지 — 서비스 인터페이스 호출만
- append-only 원칙: 마약류 수불부·감사 로그·접속기록은 UPDATE/DELETE 불가

---

## 2. Architecture

### 2.1 Component Diagram

```
                        병원 격리망 (HIS 구동 서버, Linux + Docker)
┌──────────────────────────────────────────────────────────────────────────┐
│  ┌─────────────┐   ┌──────────────────────────────────┐                  │
│  │  apps/web    │──▶│  services/core (FastAPI)         │   ┌───────────┐ │
│  │  Next.js     │   │  ┌─ common ──── hr ──── finance ┐│──▶│ 기존 DB    │ │
│  │  (브라우저)  │   │  ├─ scm ─── patient ─── claims  ││   │ (전용     │ │
│  └─────────────┘   │  └─ regulatory ─── costing(2차) ┘│   │  스키마/   │ │
│                    └────────────┬─────────────────────┘   │  전용 계정)│ │
│                                 │ Redis Streams           └───────────┘ │
│                    ┌────────────┴─────────────┐               ▲          │
│                    ▼                          ▼               │          │
│  ┌──────────────────────────┐  ┌──────────────────────────┐  │          │
│  │ services/worker-          │  │ services/worker-batch    │──┘          │
│  │ integration (ACL 어댑터)  │  │ (배치 잡)                │             │
│  │ · HIS 수신               │  │ · 수가 마스터 동기화      │             │
│  │ · 그룹웨어 출퇴근 수신    │  │ · 통보서 파싱(G1)        │             │
│  │ · 통보서 파일 수집        │  │ · 간호등급 일배치(G2)    │             │
│  │ · AI 색인 발신+폴링      │  │ · AI 정합성 대사(주1회)  │             │
│  └──────────┬───────────────┘  └──────────┬───────────────┘             │
│             │ (로컬: HIS 동일 서버)        │ (아웃바운드: 프록시 경유)    │
└─────────────┼──────────────────────────────┼────────────────────────────┘
              ▼                              ▼  HTTPS_PROXY (화이트리스트)
   [HIS EMR/OCS·청구모듈]        [공공 수가 API] [example.org AI] [그룹웨어*]
                                  * 그룹웨어 위치는 M0a와 함께 확인
```

### 2.2 Data Flow (핵심 4흐름)

```
F1 수납→전표:   Patient.수납확정 ──(동기, 단일 Tx)──▶ Finance.VoucherService.create()
F2 통보서→삭감: 파일 수집(worker-integration) → claims_staging(raw) → 파싱·검증(worker-batch)
               → claim_adjustments 반영 → [확정 시] Finance.VoucherService.create(손실 전표)
F3 고시→단가:   공공 API(worker-batch, 멱등) → fee_master_staging → 검증 → fee_master(temporal 적재)
               → 전 모듈은 FeeLookupService.get_price(code, at_date)로만 조회
F4 공시→AI:    공시 등록/개정/폐지(core) ─이벤트─▶ ai_dispatch_queue → FR-53 필터 게이트 통과 시
               worker-integration 발신(REST) → 폴링으로 색인 상태 확인 (30초/immediate)
```

### 2.3 컨테이너 토폴로지 (docker-compose)

| 서비스 | 이미지 | 리소스 상한* | 비고 |
|--------|-------|------------|------|
| web | Next.js standalone | cpu 0.5 / mem 512M | |
| core | FastAPI (uvicorn) | cpu 1.0 / mem 1G | |
| worker-integration | 동일 베이스 | cpu 0.5 / mem 512M | |
| worker-batch | 동일 베이스 | cpu 1.0 / mem 1G | **배치 스케줄: 진료시간(08–18시) 외만** |
| redis | redis:7-alpine | cpu 0.25 / mem 256M | AOF 활성 |
| (DB) | **없음 — 기존 DB 서비스 사용** | - | 전용 계정·스키마 (§7.1) |

> *상한값은 M0b 가용 리소스 확인 후 확정. HIS 영향 최소화가 최우선 — 합계가 서버 여유 자원의 50%를 넘지 않도록.

---

## 3. Data Model

> G4: PostgreSQL 15+ 전제. 모듈별 논리 스키마 분리: `common`, `hr`, `finance`, `scm`, `patient`, `claims`, `regulatory`, `integration`(스테이징), `audit`. ERP 전용 DB 계정은 위 스키마에만 권한 (HIS 스키마 접근 불가).

### 3.1 ERD (모듈 경계 — 핵심 엔티티)

```
[common]                  [hr]                        [regulatory]
 users ──────────────────▶ employees                   fee_master (temporal)
 departments ◀──────────── ├ appointments(발령)        notices ─ notice_revisions
 notifications             ├ licenses                  notice_attachments
 (audit) audit_log         ├ leave_grants/requests     ai_dispatch_queue/log
         access_log        ├ duty_schedules            labor_rules (G3 stub)
                           ├ duty_assignments          nursing_grade_config (G2 stub)
                           └ payroll_records
[patient]                 [claims]                    [scm]
 patients                  claim_batches(접수번호)     items ─ lots(유효기간)
 receptions(접수)          ├ claim_statements(명일련)  stock_movements (FIFO)
 payments(수납) ┄┄┐        ├ claim_adjustments(조정)   narcotics_ledger (append-only)
 reservations     ┆        └ appeals(이의신청+기한)    purchase_orders ─ po_lines
 daily_closings   ┆ via VoucherService ┆ via VoucherService    suppliers
                  ▼                    ▼
[finance]  vouchers ◀── VoucherService 인터페이스 경유만 (점선 = 직접 FK·조인 없음, source_ref로 추적)
           ├ voucher_lines    accounts(계정과목)    budgets
[integration] his_inbound_staging · groupware_attendance_staging · hira_notice_staging · fee_master_staging
```

### 3.2 핵심 DDL — Regulatory (시스템의 심장)

```sql
-- 수가/약제/치료재료 temporal 마스터 (FR-34/35)
CREATE TABLE regulatory.fee_master (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code          VARCHAR(20)  NOT NULL,             -- 수가/약제/재료 코드
  code_type     VARCHAR(16)  NOT NULL,             -- TARIFF | DRUG | MATERIAL
  name          TEXT         NOT NULL,
  unit_price    NUMERIC(12,2) NOT NULL,
  price_cap     NUMERIC(12,2),                     -- 급여 상한가
  criteria_text TEXT,                              -- 인정 기준 (원문)
  valid_from    DATE NOT NULL,                     -- 고시 적용일 (inclusive)
  valid_to      DATE,                              -- NULL = 현행 (exclusive)
  source        VARCHAR(8) NOT NULL DEFAULT 'API', -- API | MANUAL
  synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_fee_version UNIQUE (code, code_type, valid_from),  -- 동일 코드·날짜 복수 버전 방어 (FR-35 ③)
  CONSTRAINT ck_fee_range CHECK (valid_to IS NULL OR valid_to > valid_from),
  -- 기간 겹침 방어 — UNIQUE로는 겹침을 못 막음 (예: 1/1~ 과 2/1~3/1 동시 활성). PG 전용(G4):
  CONSTRAINT ex_fee_overlap EXCLUDE USING gist (
    code WITH =, code_type WITH =,
    daterange(valid_from, COALESCE(valid_to, 'infinity'::date), '[)') WITH &&
  )  -- requires: CREATE EXTENSION btree_gist (0001 마이그레이션 포함)
);
CREATE INDEX ix_fee_lookup ON regulatory.fee_master (code, code_type, valid_from DESC);

-- 동기화 배치 규칙 (close-then-open race 방어):
--   ① 코드 1건의 "구 버전 valid_to 닫기 + 신 버전 INSERT"는 단일 트랜잭션
--   ② 멱등키: fee_sync_runs(run_id, source_version) — 동일 source_version 재실행 시 no-op
--   ③ 실행 단위 advisory lock (pg_advisory_xact_lock) — 동기화 잡 중복 기동 방어

-- 시점 조회 (FR-35 경계 케이스 확정):
--   ① valid_from은 inclusive(>=), valid_to는 exclusive(<) — 시행일 당일은 "신 버전" 적용
--   ② valid_to IS NULL은 현행 버전 — at_date >= valid_from이면 매치
-- SELECT * FROM regulatory.fee_master
--  WHERE code=$1 AND code_type=$2 AND valid_from <= $at AND (valid_to IS NULL OR valid_to > $at);

-- 공시 원문 (FR-51)
CREATE TABLE regulatory.notices (
  erp_ref_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- 불변 ID (AI 동기화 키)
  notice_type   VARCHAR(16) NOT NULL,  -- TARIFF|DRUG_MATERIAL|REVIEW_CRITERIA|MEDICAL_LAW|ADMIN|NARCOTIC|LABOR|INTERNAL
  current_rev   INT NOT NULL DEFAULT 1,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE regulatory.notice_revisions (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  erp_ref_id    UUID NOT NULL REFERENCES regulatory.notices,
  revision_no   INT  NOT NULL,
  title         TEXT NOT NULL,
  body_text     TEXT NOT NULL,                     -- UTF-8 정규화 텍스트, 1MB 검증은 앱 레이어
  content_hash  CHAR(64) NOT NULL,                 -- SHA-256 (정규화 7항 — 연동 스펙 v1.0)
  effective_from DATE NOT NULL,                    -- 필수 (등록 차단 규칙)
  effective_to   DATE,
  status        VARCHAR(12) NOT NULL,              -- SCHEDULED | ACTIVE | REPEALED
  created_by    BIGINT NOT NULL,                   -- common.users.id
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_notice_rev UNIQUE (erp_ref_id, revision_no)
);

-- AI 발신 큐 (FR-52/53)
CREATE TABLE regulatory.ai_dispatch_queue (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  erp_ref_id        UUID NOT NULL,
  revision_no       INT NOT NULL,
  event_type        VARCHAR(8) NOT NULL,   -- CREATE | UPDATE | REVOKE
  urgency           VARCHAR(10) NOT NULL,  -- IMMEDIATE | BATCH
  client_request_id UUID NOT NULL DEFAULT gen_random_uuid(),
  filter_status     VARCHAR(12) NOT NULL DEFAULT 'PENDING',  -- PENDING|PASSED|BLOCKED|APPROVED (FR-53)
  filter_detail     JSONB,                 -- 탐지 패턴·위치 (차단 사유)
  dispatch_status   VARCHAR(10) NOT NULL DEFAULT 'QUEUED',   -- QUEUED|SENT|DONE|FAILED|DLQ
  retry_count       INT NOT NULL DEFAULT 0,
  triggered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),      -- SLA 기산점 (DB 커밋)
  sent_at           TIMESTAMPTZ,
  response_code     INT,
  elapsed_ms        INT
);
```

### 3.3 핵심 DDL — Claims (G1: 매핑 스펙은 보류, 구조는 확정)

```sql
CREATE TABLE claims.claim_batches (          -- 통보서 접수번호 단위
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  receipt_no    VARCHAR(20) NOT NULL UNIQUE, -- 접수번호
  care_month    CHAR(7) NOT NULL,            -- 진료년월 YYYY-MM
  decided_on    DATE,                        -- 심사결정일 (§10 *_on 컨벤션)
  notice_arrived_on DATE NOT NULL,           -- 도달일 — FR-40 기한 기산
  staging_id    BIGINT,                      -- 출처 스테이징 추적
  claimed_amt   NUMERIC(14,0), decided_amt NUMERIC(14,0),
  source_file   TEXT, received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE claims.claim_statements (       -- 명세서(명일련번호) 단위
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_id      BIGINT NOT NULL REFERENCES claims.claim_batches,
  stmt_serial   VARCHAR(10) NOT NULL,        -- 명일련번호
  chart_no      VARCHAR(20),                 -- 환자 연결 키 (patient 모듈 조인 금지 — 서비스 조회)
  dept_code     VARCHAR(10), doctor_id BIGINT,
  claimed_amt   NUMERIC(14,0), adjusted_amt NUMERIC(14,0), decided_amt NUMERIC(14,0),
  CONSTRAINT uq_stmt UNIQUE (batch_id, stmt_serial)
);
CREATE TABLE claims.claim_adjustments (      -- 조정(삭감) 내역
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  statement_id  BIGINT NOT NULL REFERENCES claims.claim_statements,
  adj_code      VARCHAR(10),                 -- 조정사유코드 (G1: 코드 마스터는 M0c 후)
  adj_category  VARCHAR(20),                 -- 분류: 약제|재료|행위|기타 (자동 분류 결과)
  adj_amt       NUMERIC(14,0) NOT NULL,
  adj_reason    TEXT,
  map_status    VARCHAR(10) NOT NULL DEFAULT 'AUTO'  -- AUTO|MANUAL|UNMAPPED (자동화율 분자/분모)
);
CREATE TABLE claims.appeals (                -- 이의신청 (FR-40)
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  adjustment_id BIGINT NOT NULL REFERENCES claims.claim_adjustments,
  appeal_type   VARCHAR(16) NOT NULL,        -- REVIEW_REQUEST(재심사조정청구) | OBJECTION(이의신청)
  status        VARCHAR(12) NOT NULL DEFAULT 'RECEIVED', -- RECEIVED|SUPPLEMENT|REJECTED|ACCEPTED
  notice_arrived_on DATE NOT NULL,           -- 통보서 도달일 (입력 신뢰성: 파일 수신일 자동 + 수동 보정 시 감사 기록)
  legal_deadline    DATE NOT NULL,           -- 도달일 + 90일 — ⚠️ G5: 역일 가정, 영업일·공휴일 규칙은 확인 후 산정 함수 교체
  final_loss_amt    NUMERIC(14,0),           -- 확정 시 손실 전표 생성 (F2)
  voucher_ref       VARCHAR(40)              -- Finance 전표 추적 키 (FK 아님)
);
```

### 3.4 핵심 DDL — SCM 마약류 (append-only, FR-20)

```sql
CREATE TABLE scm.narcotics_ledger (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  seq_no        BIGINT NOT NULL,             -- 품목별 연속 순번 (무결성 검증)
  item_id       BIGINT NOT NULL,             -- scm.items (마약류 플래그)
  lot_id        BIGINT,
  entry_type    VARCHAR(10) NOT NULL,        -- IN|OUT|DISPOSE|ADJUST|CORRECTION
  qty           NUMERIC(12,2) NOT NULL,
  balance_after NUMERIC(12,2) NOT NULL,
  handler_id    BIGINT NOT NULL,             -- 취급자 (users)
  corrects_id   BIGINT REFERENCES scm.narcotics_ledger,  -- CORRECTION일 때 원본 참조
  correction_reason TEXT,                    -- CORRECTION 필수
  row_hash      CHAR(64) NOT NULL,           -- 해시 체인: SHA-256(prev_hash || row 데이터)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_narc_seq UNIQUE (item_id, seq_no)
);
-- DB 레벨 강제: UPDATE/DELETE 차단 (감사 테이블도 동일 — audit.audit_log, audit.access_log에 REVOKE.
--  보존연한 파기는 erp_app이 아닌 별도 권한 계정 + 승인 흐름으로만)
REVOKE UPDATE, DELETE ON scm.narcotics_ledger FROM erp_app;  -- 앱 계정에 INSERT/SELECT만

-- 동시성·무결성 규칙:
--   ① seq_no·row_hash 채번은 품목 단위 직렬화: pg_advisory_xact_lock('narc', item_id) 후 INSERT
--      (동시 INSERT로 체인 분기 방지 — uq_narc_seq는 최후 방어선)
--   ② balance_after는 사용자 입력 금지 — 서비스가 직전 행 기준 계산 (correct API는 수량만 받음)
--   ③ 정정 규칙: corrects_id는 원본(IN/OUT/DISPOSE/ADJUST)만 참조. 정정을 다시 정정할 때도
--      "원본"을 참조하는 새 CORRECTION 추가 — 최신 CORRECTION만 유효 (한 원본의 유효 정정 1개)
--   ④ 무결성 검증 잡(주 1회): seq_no 순 balance 재계산 + 해시 체인 연속성 검사 → 불일치 시 관리자 알림
```

### 3.5 기타 모듈 핵심 테이블 (요약)

| 스키마 | 테이블 | 핵심 컬럼·규칙 |
|--------|--------|---------------|
| common | `users` | email(uq), password_hash(argon2id), role, employee_id, license_flag_active, **token_version**(INT — 강제 무효화), failed_attempts, locked_until |
| common | `refresh_tokens` | user_id, token_hash(SHA-256, uq), family_id(회전 체인), issued_at, expires_at, revoked_at, replaced_by — **회전·재사용 탐지의 저장소** (재사용 탐지 시 family 전체 폐기) |
| common | `departments`, `positions` | 트리 구조(parent_id), 부서장 user_id |
| common | `notifications` | user_id, type, payload, read_at — In-app 알림 (FR-26) |
| audit | `audit_log` | append-only — actor, action, entity, before/after(JSONB), at |
| audit | `access_log` | append-only, **2년 보존** — actor, action(VIEW/EXPORT/PRINT), entity, target_id, route, at. **기록은 응답 경로 밖 비동기**(Redis Stream 적재 → 워커 기록, 적재 실패 시 동기 폴백 — 유실 금지) |
| hr | `employees` | 사번(uq), 인적사항, 입·퇴사일, department_id — 주민번호 미저장 |
| hr | `appointments` | 발령 이력 (employee_id, type, from/to dept, effective_on) |
| hr | `licenses` | 면허 종류, 번호, expires_on(만료 알림 D-30/D-7), 보수교육 이수 |
| hr | `leave_grants` / `leave_requests` | 연차 부여(근로기준법 산정 — FR-31)·신청 워크플로우(status) |
| hr | `duty_schedules` / `duty_assignments` | 월 단위 schedule + 일·직원별 D/E/N/OFF. 저장 시 G3 룰 평가 → 경고 목록 반환 |
| hr | `payroll_records` / `payroll_lines` | 헤더(월·직원·confirmed_at) + **라인(item_code FK→급여항목 마스터, amount)** — JSONB 대신 라인 테이블 (항목별 집계·검증·감사 추적 가능). 확정 후 수정 시 감사 기록 |
| hr | `attendance_summaries` | 월·직원별 근태 집계 (FR-29) — 지각·조퇴·야간근무 판정 결과, 그룹웨어 수신 데이터 기반 일배치 산출 |
| finance | `accounts` | 계정과목 (코드 트리) |
| finance | `vouchers` / `voucher_lines` | 전표 + 분개. **source_module + source_ref**로 출처 추적 (Patient/Claims/SCM) |
| finance | `budgets` | 부서·연도·계정과목별 편성액, 집행 집계는 뷰 |
| (claims) | 청구 상태 단일 소스 = **claims 모듈** (FR-10) — finance는 별도 테이블 없이 `claims/interfaces.py: ClaimsStatusReader`로 조회 (중복 책임 제거) |
| patient | `patients` | 이름, birth_date, **chart_no(uq)**, phone(AES-GCM 암호화) + **phone_bidx**(HMAC blind index — 전체 일치 검색용, §6.4) — FR-33 최소화 + last_visit_on |
| patient | `receptions`, `payments` | 접수·수납. 수납 확정 시 F1 전표 흐름 |
| patient | `reservations` | 진료과/의사·시간대 (중복 방지 제약) |
| patient | `daily_closings` | 일 마감 — 마감 시 Finance 전표 생성 + 잠금. **마감 후 정정 경로(FR-54)**: 마감 데이터 직접 수정 금지 — 정정 수납 + **역분개 전표**(VoucherService.reverse) + 재마감 레코드 (원 마감 보존) |
| scm | `items`, `lots` | 품목(safety_stock_qty, is_narcotic), 로트(lot_no, expiry_date 필수) |
| scm | `stock_movements` | 입출고 — 출고는 FIFO 엔진이 lot 자동 선택, 유효기간 3개월 미만 lot 출고 차단(관리자 override 기록). **동시성**: lot 선택은 `SELECT ... FOR UPDATE`(유효기간·입고일 순 — **SKIP LOCKED 금지**, FIFO 순서 깨짐) + `lots.remaining_qty CHECK (>= 0)` 음수 방어 |
| scm | `purchase_orders` / `po_lines` | 발주 헤더(status: 요청→승인→입고, supplier_id) + 라인(item_id, qty, unit_price) |
| scm | `suppliers` | 공급업체 마스터 (사업자번호 uq, 담당자·연락처) |
| integration | `*_staging` | 모든 외부 수신 raw 보관 + 처리 상태 + 오류 상세. 상태값: 통보서(`hira_notice_staging`)는 NEW/OK/FAILED/REPROCESSED(파서 재소비), ACL 어댑터(`groupware_attendance`/`his_patient`/`his_payment`)는 NEW/APPLIED/FAILED(재처리는 FAILED→NEW) |

---

## 4. API Specification

### 4.1 공통 규약

- Base: `/api/v1/{module}/...`, JSON, JWT Bearer
- 페이지네이션: `?page=1&size=50` (max 200) → `{ items, total, page, size }`
- 에러 포맷: `{ "error": { "code": "ERP-XXXX", "message": "...", "details": {} } }` — 스택트레이스 미노출
- 목록 응답 < 500ms 목표, 전 목록 엔드포인트 페이지네이션 필수

### 4.2 에러 코드 체계

| 코드 | 의미 |
|------|------|
| ERP-1xxx | 인증/권한 (1001 인증 실패, 1002 계정 잠금, 1003 토큰 만료, 1004 권한 없음, 1005 면허 플래그 제한) |
| ERP-2xxx | 검증 (2001 필수 필드, 2002 형식, 2003 도메인 규칙 위반 — 예: 시행일 누락 공시) |
| ERP-3xxx | 비즈니스 (3001 FIFO 재고 부족, 3002 유효기간 출고 차단, 3003 듀티 규제 위반[경고], 3004 이의신청 기한 경과, 3005 마감 후 수정 불가) |
| ERP-4xxx | 연계 (4001 스테이징 검증 실패, 4002 AI 발신 차단[FR-53], 4003 외부 API 실패) |

### 4.3 엔드포인트 목록 (Phase 1 — 모듈별 대표)

| Module | Method·Path | 설명 | 권한(§6) |
|--------|------------|------|---------|
| common | `POST /auth/login` · `POST /auth/refresh` · `POST /auth/logout` | 인증 (FR-01: 잠금·rate limit) | 공개/본인 |
| common | `GET /notifications` · `PATCH /notifications/{id}/read` | 알림 센터 | 본인 |
| common | `GET /admin/audit-logs` · `GET /admin/access-logs` | 감사·접속기록 조회 | 원장·관리자 |
| common | `GET·POST·PATCH /departments` | 부서(조직) 관리 — 정책 키 `common.departments` | 관리자 |
| common | `GET /export/{resource}.xlsx` | 공통 내보내기 (FR-22 — 목록 서비스 재사용·scope 합성·EXPORT 접속기록. 렌더링: openpyxl write_only §11.0) | 매트릭스 |
| hr | `GET·POST·PATCH /employees` (+`/­{id}/appointments`) | 직원·발령 | 매트릭스 |
| hr | `GET·POST /licenses` | 면허 (만료 알림은 배치) | 매트릭스 |
| hr | `POST /leaves/requests` · `PATCH /leaves/requests/{id}/approve\|reject` · `GET /leaves/remaining` | 휴가 워크플로우 + 잔여 조회 | 본인/부서장 |
| hr | `GET·PUT /duties/{yyyymm}` → 저장 응답에 `violations[]` (G3 룰 평가) | 듀티 편성 | 부서장 |
| hr | `GET /payrolls/me/{yyyymm}` · `POST /payrolls/{yyyymm}/confirm` | 급여 조회/확정 | 본인/관리자 |
| hr | `GET /payrolls/me/{yyyymm}/pdf` → PDF | 급여명세서 PDF (FR-07b — WeasyPrint §6.7·§11.0, PRINT 접속기록) | 본인 |
| hr | `GET /attendance?employee=&yyyymm=` | 근태 집계 조회 (FR-29 — 지각·야간 판정 결과) | 본인/부서장/관리자 |
| hr | `POST /certificates` → PDF | 증명서 발급 (발급 이력 + access_log) | 본인 |
| regulatory | `GET /fees?code=&at=` | **시점 단가 조회 (전 모듈 공용)** | 인증 |
| regulatory | `POST·PUT /notices` · `POST /notices/{id}/revoke` | 공시 관리 (FR-51 — 시행일 필수 검증) | 관리자 |
| regulatory | `GET /notices/{id}/dispatch-status` | AI 색인 발신·필터 상태 | 관리자 |
| claims | `POST /claims/upload` | 통보서 파일 업로드 (fallback 경로 — 필수 구현) | 의료보험팀 |
| claims | `GET /claims/batches` · `GET /claims/statements?batch=` | 통보서·삭감 조회 | 의료보험팀 |
| claims | `POST /appeals` · `PATCH /appeals/{id}/status` | 이의신청 (기한 자동 계산·D-임박 알림) | 의료보험팀 |
| claims | `GET /claims/stats?by=dept\|doctor` | 삭감 통계 (FR-48) | 매트릭스 |
| scm | `GET·POST /items` · `POST /movements/receive` · `POST /movements/issue` | 품목·입출고 분리 (FIFO·차단·override는 관리자만) | 담당 |
| scm | `POST /narcotics/entries` · `POST /narcotics/entries/{id}/correct` | 마약류 수불 (append-only) | 약제 담당 |
| scm | `GET /narcotics/ledger/export?yyyymm=` | 수불일지 Excel/PDF (FR-21) | 약제 담당 |
| scm | `POST /purchase-orders` · `PATCH .../approve` · `POST .../receive` | 발주 워크플로우 (라인 포함) | 매트릭스 |
| scm | `GET·POST·PATCH /suppliers` | 공급업체 관리 | 담당 |
| patient | `GET·POST /patients` (검색: name/birth/chart_no) | 환자 (FR-33 4필드) | 매트릭스 |
| patient | `POST /receptions` · `POST /payments` · `POST /closings/{date}` | 접수·수납·일마감(F1 전표) | 원무 |
| patient | `POST /closings/{date}/amend` | **마감 후 정정 (FR-54)** — 정정 수납 + 역분개 + 재마감 (원 마감 보존, 감사 기록) | 관리자 |
| admin | `POST /admin/staging/{id}/reprocess` | 실패 행 재처리 (FAILED만, audit 기록, source_ref 멱등) | 관리자 |
| patient | `GET·POST /reservations` | 예약 캘린더 | 원무 |
| finance | `GET·POST /vouchers` · `GET /accounts` · `GET·PUT /budgets` · `GET /budgets/execution` | 전표·예산·집행률(FR-09) | 관리자 |
| finance | `GET /reports/monthly/{yyyymm}` | 월별 정산 | 매트릭스 |
| 공용 | `GET /{module}/{resource}/export.xlsx` | Excel 내보내기 (FR-22 — access_log 기록) | 매트릭스 |

### 4.4 모듈 간 내부 인터페이스 (코드 경계 — HTTP 아님)

> **트랜잭션 전달 규약 (최상위 — 이것 없이는 "단일 Tx" 선언이 무력화됨)**:
> 모든 쓰기 인터페이스 메서드는 **첫 인자로 `session: AsyncSession`을 필수로 받는다** (Unit of Work 패턴).
> 트랜잭션은 **호출자(유스케이스)가 소유** — 인터페이스 구현체는 절대 자체 commit/rollback 하지 않고 전달받은 세션에 참여만 한다.
> 예: 수납 확정 유스케이스가 세션을 열고 → 수납 저장 + `VoucherService.create(session, ...)` → 단일 commit.
> 읽기 전용 메서드(`get_price` 등)는 세션 생략 가능 (자체 read 세션).

```python
# finance/interfaces.py — 전표 생성 단일 창구 (FR-49)
class VoucherService(Protocol):
    def create(self, session: AsyncSession, *, source_module: str, source_ref: str,
               voucher_on: date, lines: list[VoucherLineDTO], memo: str,
               created_by: int | None) -> VoucherId: ...
    # 멱등성: (source_module, source_ref) UNIQUE — 동일 출처 재호출 시 기존 전표 반환 (중복 생성 방지)
    def reverse(self, session: AsyncSession, *, voucher_id: VoucherId,
                reason: str) -> VoucherId: ...
    # 역분개 (FR-54 마감 후 정정) — 원 전표 보존 + 반대 분개 신규 생성

# regulatory/interfaces.py — 시점 단가 (FR-35)
class FeeLookupService(Protocol):
    def get_price(self, code: str, code_type: CodeType, at_date: date) -> FeeVersion: ...
    # 규칙: valid_from <= at_date < valid_to(NULL=∞). 미존재 시 FeeNotFoundError

# hr/interfaces.py — 간호등급 입력원 (G2)
class DutyDataProvider(Protocol):
    def get_nurse_counts(self, on: date, dept_id: int) -> NurseCountDTO: ...

# 공통 — 조회 인터페이스의 scope 전파 (행 필터 우회 방지):
#   목록·집계를 반환하는 모든 인터페이스 메서드는 caller_scope: Scope 를 필수 인자로 받아
#   집계·뷰 쿼리에도 명시적으로 WHERE 조건을 합성한다. BFF 대시보드는 자체 권한 없이
#   호출자 컨텍스트의 scope를 그대로 전달한다.
```

---

## 5. 외부 연계 설계 (ACL)

### 5.1 어댑터 인터페이스 (worker-integration — 구현체 교체 가능)

```python
class HISPatientSource(Protocol):        # M0a=REST 확정 → RestHISPatientSource 구현 (his.example.org)
    def fetch_patients(self, since: datetime) -> Iterator[RawPatientDTO]: ...
    def fetch_payments(self, since: datetime) -> Iterator[RawPaymentDTO]: ...
    # 구현: RestHISPatientSource(Bearer+X-Hospital-Code) — 후보였던 FileDrop/DBView/HL7은 미채택.
    #       HIS ERP-sync 엔드포인트 계약 확정 시 어댑터만 교체(스테이징·소비 불변)

class GroupwareAttendanceSource(Protocol):
    def fetch_attendance(self, since: datetime) -> Iterator[RawAttendanceDTO]: ...

class HiraNoticeSource(Protocol):        # G1 — 파서는 M0c 후
    def collect_files(self) -> Iterator[RawNoticeFile]: ...
    # 구현 후보: HIS 청구모듈 폴더 감시(유력) | 수동 업로드 API(필수 fallback)

class FeeMasterSyncSource(Protocol):     # 공공 수가 API
    def fetch_updates(self, since: date) -> Iterator[RawFeeDTO]: ...

class AIIndexPublisher(Protocol):        # 연동 스펙 v1.0
    def publish(self, event: NoticeEvent) -> PublishResult: ...        # POST/PUT/revoke
    def poll_status(self, client_request_id: UUID) -> IndexStatus: ...
    def sync_verify(self, snapshot: list[NoticeDigest]) -> list[Mismatch]: ...
```

### 5.2 수신 파이프라인 공통 규칙

1. raw → `integration.*_staging` 저장 (원문 보존, 처리 상태 NEW)
2. 검증·매핑 (worker-batch) — 실패 행은 FAILED + 오류 상세, **성공 행만 코어 반영**
3. 실패 행 재처리 API: `POST /admin/staging/{id}/reprocess` (E2E ⑥ 경로) — **관리자 한정, audit_log 기록, FAILED 상태 행만 허용**. 다운스트림은 source_ref 멱등(전표 등 중복 생성 불가)
3-1. **멱등성 (Redis at-least-once 대응)**: 모든 워커 잡은 멱등키 기반 — AI 발신은 `ai_dispatch_queue.id` 상태 CAS 전이, 파싱은 staging 행 상태 조건부 처리, 전표는 (source_module, source_ref) UNIQUE — **중복 메시지 소비가 중복 부작용을 만들 수 없는 구조**
4. 모든 외부 HTTP: TLS 검증 강제, `HTTPS_PROXY` 경유, 도메인 화이트리스트 상수로 한정(SSRF 차단)
5. **request_id 전파 규약** (NFR 관측성): HTTP 진입 시 미들웨어가 `request_id`(UUID) 생성 → Redis Streams 발행 시 메시지 메타 필드 `{"request_id": ..., "origin": "core|scheduler"}` 포함 → 워커 잡은 컨텍스트(contextvar)에 주입해 모든 로그에 출력. 스케줄 기동 잡은 `job:{잡이름}:{ts}` 형식으로 자체 생성

### 5.3 AI 발신 파이프라인 (FR-52/53)

```
공시 커밋 → ai_dispatch_queue(triggered_at)
  → [FR-53 필터 게이트] — CREATE/UPDATE/REVOKE 모든 이벤트에 적용:
      전처리: NFC 정규화 + 공백/구분자/zero-width 제거 + 동형문자(confusable) 정규화 후 매칭
      탐지: (a)패턴(주민번호·연락처·차트번호·사번) + (b)주민번호 체크섬 검증 + (c)실데이터 사전 대조(차트번호·사번)
      → 탐지 시 BLOCKED + 관리자 검토 큐. 승인(APPROVED)은 해당 content_hash에 바인딩 —
        발신 직전 현재 본문 hash 불일치 시 자동 재필터·재승인 요구
      → INTERNAL 분류: 무조건 관리자 승인 필요
      → 첨부파일: 기본 발신 제외 (발신 필요 시 텍스트 추출 후 동일 필터 통과 필수)
  → 발신 (재시도 3회 지수 백오프) → 실패 시 DLQ + 알림 (E2E ⑦ 경로)
  → IMMEDIATE: 30초 간격 폴링, 5분 내 done 미확인 시 경고
배치: 일 1회(야간) + 주 1회 sync_verify (일요일 02:00–06:00)
단위 테스트: 우회 벡터 케이스 필수 — 공백 삽입·전각 숫자·zero-width·특수 대시·체크섬 유효/무효
```

---

## 6. 보안 설계

### 6.1 인증 (FR-01)

| 항목 | 설계 |
|------|------|
| Access Token | JWT, **30분** 만료. payload: user_id, role, dept_id, token_version, license_flag |
| Refresh Token | **7일**, DB 저장(해시), 회전 — **원자적 CAS**: `UPDATE ... SET revoked_at=now(), replaced_by=:new WHERE token_hash=:h AND revoked_at IS NULL RETURNING id` → 영향 0행이면 재사용으로 판정. 직전 회전 토큰은 수 초 grace window(동시 탭 오탐 방지), grace 밖 재사용 시 family 전체 폐기 |
| 강제 무효화 | `users.token_version` 증가 → 미들웨어 검증은 **Redis 캐시 우선**(`user:{id}:tv`, 변경 Tx에서 즉시 del — write-through) → DB 부하 없이 다음 요청부터 차단. Redis 장애 시 DB 폴백, 30분 만료는 fail-safe 상한 |
| 비밀번호 | argon2id, 최소 10자+복잡도, 직전 3개 재사용 금지 |
| 잠금 | 실패 5회 → 15분 잠금 (ERP-1002), `/auth/*` rate limit 10req/min/IP |
| 감사 | 로그인 성공/실패·토큰 갱신·잠금 → audit.access_log |
| MFA | Phase 1 보류 — 원장·관리자 TOTP는 Phase 2 검토 (Plan FR-01) |

### 6.2 인가 — RBAC + 행 수준 필터

- FastAPI dependency 체인: `requires(module, action)` → 역할 매트릭스 평가 (Plan §3.3 코드화: `common/policy.py`의 단일 정책 테이블)
- 행 수준: `scope=ALL | DEPT(본부서) | SELF(본인)` 를 정책 테이블에 정의. **"자동 주입" 단독 의존 금지** — 모든 조회 서비스 메서드가 `caller_scope`를 필수 인자로 받아 집계·raw SQL·DB 뷰 쿼리에도 명시적 WHERE 합성 (§4.4 규약). DB 뷰 직접 접근 금지(서비스 경유만). Excel export는 목록 조회 서비스를 **재사용**(별도 쿼리 금지). 2차 방어로 PostgreSQL RLS 정책 적용 검토(G4)
- 면허 플래그: `license_flag_active=false`(면허 만료) 직원은 **면허필요 임상행위** 차단 (FR-47, ERP-1005). 현 적용 = 마약류 취급·정정(`scm.narcotics` write). ERP 내 휴가·성과급·전표 "결재"는 임상 면허와 무관(비대상)이며 전자결재 본체는 외부 그룹웨어가 수행. fail-open(lic 클레임 부재 시 통과)은 가용성 의도 — 만료 즉시 강제는 token_version 무효화로 재로그인 유도
- 민감 조회·반출(환자/급여/Excel/PDF)은 미들웨어가 access_log 자동 기록

### 6.3 HIS 보안 경계 (Plan NFR)

- DB: `erp_app` 전용 계정 — `common/hr/finance/scm/patient/claims/regulatory/integration/audit` 스키마만 GRANT. HIS 스키마 권한 0. 마약류 테이블은 UPDATE/DELETE REVOKE
- 컨테이너: 전용 docker network, HIS 디렉터리 미마운트 (HIS 연계가 파일 방식이면 **교환 전용 디렉터리만** read-only 마운트), no-new-privileges, 비 root 실행
- ERP↔HIS 통신은 §5.1 어댑터 인터페이스로만

### 6.4 저장 암호화 대상 (NFR 저장 암호화 — 컬럼 레벨)

| 항목 | 컬럼 | 방식 |
|------|------|------|
| 비밀번호 | `users.password_hash` | argon2id (단방향) |
| 환자 연락처 | `patients.phone` | AES-256-GCM (아래 포맷) — 조회 시 마스킹 기본, 권한자만 복호 |
| 직원 연락처·주소 | `employees.phone/address` | AES-256-GCM 동일 |
| 전화번호 검색 | `patients.phone_bidx` | **HMAC-SHA256 blind index** (정규화 후 전체 일치 검색 전용 — 부분 검색 미지원). 키는 DATA_ENC_KEY와 **별도**(`PHONE_BIDX_KEY`) |
| Refresh Token | `refresh_tokens.token_hash` | SHA-256 (원문 미저장) |
| DB at-rest | M0f 결정 보류 (기존 DB 서비스 설정 의존) | |

**GCM 저장 포맷·운영 규칙 (필수 준수)**:
- 컬럼 BYTEA 레이아웃: `key_version(1B) ‖ nonce(12B) ‖ ciphertext ‖ tag(16B)`
- **nonce는 매 암호화마다 `os.urandom(12)` 신규 생성 — 재사용·카운터 파생 절대 금지** (재사용 시 평문 복구 가능)
- AAD = `(schema.table.column, row_pk)` 바인딩 → 행 간/컬럼 간 암호문 치환 공격 탐지
- **암호화 컬럼은 등가·부분·LIKE 검색 대상이 아님** — 검색 필요 시 blind index만 (평문 보조 컬럼 절대 금지)
- 키 회전 (분기): ① 신 key_version으로 신규 쓰기 → ② worker-batch 야간 잡으로 구 버전 행 점진 재암호화 → ③ 전 행 전환 + 백업 세대 만료 후 구 키 폐기, 회전 이력 audit 기록
- 키 보관: env 평문 대신 **권한 600 파일 마운트, 기동 시 로드 후 환경에서 제거**. JWT_SECRET·DATA_ENC_KEY·PHONE_BIDX_KEY 모두 분리

### 6.5 백업/DR 설계 (NFR 백업 보안)

- **대상**: ERP 전용 9개 스키마 한정 `pg_dump --schema=...` 일배치 (worker-batch, 22:00) — HIS 스키마 비포함
- **암호화**: dump 직후 `age`/GPG 공개키 암호화 → 오프사이트 반출. **복호 개인키는 백업 매체와 분리 보관** (관리자 금고)
- **WAL**: 기존 DB 서비스 운영 주체와 책임 분담 M0b에서 확정 (ERP는 논리 백업 책임, 인스턴스 PITR은 DB 운영 측)
- **보존·파기**: 백업 세대 — 일 7세대·월 12세대·연 5세대(환자 5년 정합). 만료 백업 파기 + `audit.audit_log`에 파기 이력 기록
- **데이터 파기 잡** (NFR Data Retention): worker-batch 월 1회 — 보존연한 경과 데이터(환자 last_visit+5년, 퇴직+3년 급여·근태, 접속기록 2년) 식별 → 관리자 승인 후 파기 + 파기 이력 기록
- **복구 리허설**: Phase 3 — 암호화 백업 복호→복원→무결성(마약류 해시 체인 검증 포함) 점검

### 6.6 응답 보안 헤더·CORS

- 보안 헤더 (단일 소스: FastAPI 미들웨어 — Next.js는 정적 자산만): `Strict-Transport-Security`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `CSP: default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'`, `Permissions-Policy`(카메라·마이크·지오 차단), `Referrer-Policy: no-referrer`
- CORS: 동일 오리진 전제(web→core는 내부 리버스 프록시 경유) — 허용 오리진 화이트리스트 환경 변수 1개(`WEB_ORIGIN`)만

### 6.7 파일 입출력 보안

**통보서 업로드 (`POST /claims/upload` — 유일한 인바운드 파일 경로)**:
- 크기 상한 50MB, 허용 확장자 + **매직바이트 검증** (확장자 단독 신뢰 금지)
- 저장 파일명은 **서버 생성 UUID로 재명명** (원본명은 메타데이터만 — 경로 탐색 차단), 저장 경로 = 고정 prefix + UUID만
- 저장 위치: staging 전용 볼륨 (core·HIS와 분리, **no-exec 마운트**)

**Export 산출물 (Excel/PDF 비동기 잡)**:
- 다운로드 = 인증 필수 엔드포인트 + **요청자(소유자) 검증** + 잡 만료 24h — 추측 불가 토큰이어도 인증·소유자 확인 병행 (IDOR 차단)
- 다운로드 시 access_log(EXPORT) 기록, 산출물은 격리 볼륨 + 만료 후 파기 잡 삭제
- 마약류 수불일지 export는 "사본은 임시, 원장은 DB" 원칙 (법정 기록과 비충돌)

**PDF 렌더링 (WeasyPrint)**:
- **커스텀 url_fetcher 강제**: 번들된 로컬 폰트·이미지 화이트리스트(절대경로 prefix)만 허용, `file://`·`http(s)://`·`data:` 전부 예외 발생 (LFI/SSRF 차단 — WeasyPrint는 url()을 실제 fetch함)
- Jinja2 `autoescape=True` 고정, 사용자 입력 변수에 `| safe` 금지 (코드 리뷰 체크 항목)

---

## 7. UI/UX Design

### 7.1 화면 목록 (Phase 1 — 28화면)

| 모듈 | 화면 | 라우트 |
|------|------|--------|
| 공통 | 로그인 / 대시보드(역할별 위젯) / 알림 센터 / 감사·접속기록(관리자) / 근태 집계 조회 | `/login` `/dashboard` `/notifications` `/admin/logs` `/hr/attendance` |
| HR | 직원 목록·상세(발령/면허 탭) / 휴가 신청·승인함 / 듀티 편성(월 그리드+위반 경고 패널) / 듀티 조회(본인) / 급여명세(본인) / 급여 입력(관리자) / 증명서 발급 | `/hr/...` |
| Regulatory | 수가 마스터 조회(시점 선택기) / 공시 목록·편집기(시행일 필수) / AI 발신 모니터(필터 차단 검토 큐) | `/regulatory/...` |
| Claims | 통보서 업로드 / 통보서·삭감 내역(배치→명세→조정 드릴다운) / 이의신청 보드(기한 D-day) / 삭감 통계 대시보드 | `/claims/...` |
| SCM | 품목·재고 현황 / 입출고 등록 / 마약류 수불부(정정 모달) / 발주 보드 | `/inventory/...` |
| Patient | 환자 검색·등록 / 접수·수납 / 예약 캘린더 / 일마감 | `/patients/...` |
| Finance | 전표 목록·등록 / 예산 현황 / 월별 정산 리포트 | `/finance/...` |

### 7.2 프론트 구조

```
apps/web/src/
├── app/(auth)/login · dashboard · hr/ · regulatory/ · claims/ · inventory/ · patients/ · finance/ · admin/
├── components/ui/        # shadcn 기반 공용
├── features/{module}/    # 모듈별 컴포넌트·hooks·api 클라이언트
└── lib/api.ts            # fetch 래퍼 (토큰 갱신·에러 토스트·ERP-코드 매핑)
```

상태: TanStack Query(서버 상태) + Zustand(편성 그리드 등 로컬 복잡 상태만). 폼: react-hook-form + zod (API 스키마와 동일 소스 — packages/shared-types에서 생성).

---

## 8. Test Plan (Plan §4.4 구체화)

| 종류 | 대상 | 도구 |
|------|------|------|
| 단위 (≥70%) | temporal 조회(경계 3케이스 독립), FIFO 엔진, 연차 산정, 듀티 룰 평가(G3 확정 후), content_hash(테스트 벡터 3건), FR-53 필터 패턴, 전표 생성, 해시 체인 | pytest |
| 통합 | API 엔드포인트 + RBAC 매트릭스 표 주도 테스트(역할×액션 전수) | pytest + httpx |
| E2E 7건 | Plan §4.2 — 합성 픽스처 기반 | Playwright |
| 연계 | §5.1 어댑터별 Mock 구현체 (Plan §4.4 표) — 통보서는 익명화 픽스처(M0c 후) | pytest |
| Zero Script QA | 시나리오 목록은 구현 완료 시 확정 (구조화 JSON 로그 + Request ID 전 서비스 적용) | Phase 3 |

합성 데이터: `scripts/seed_synthetic.py` — TEST- 차트번호 환자 500, 가상 직원 150(간호 3교대 패턴), 수가 마스터 2버전(경계 테스트용), 듀티 1개월. **M1 산출물.**

---

## 9. Clean Architecture

### 9.1 백엔드 레이어 (모듈 내부 공통)

```
services/core/src/erp/{module}/
├── router.py        # Presentation — FastAPI 라우터 (검증·권한 dependency)
├── *_service.py     # Application — 유스케이스 + 타 모듈 공개 서비스 (cross-import 단위)
└── models.py        # SQLAlchemy 모델 (스키마 경계)
```
> **v0.4 개정 (Gap 분석 반영)**: `interfaces.py`/`repository.py`/`domain.py` 분리 대신
> **service 모듈의 공개 서비스 클래스가 cross-import 단위** (예: `finance.service.VoucherService`).
> 외부 연계 Protocol은 `integrations/sources.py`·`ai_publisher.py`에 집중 배치.

### 9.2 모듈 경계 임포트 규칙 (린트 강제 — import-linter 도입 시)

| From → To | 허용 |
|-----------|:---:|
| {module} → 자기 모듈 내부 | ✅ |
| {module} → 타 모듈 `*_service.py` 공개 서비스 클래스 | ✅ (유일한 경로) |
| {module} → 타 모듈 models.py 직접 import·조인 | ❌ |
| {module} → common (auth·policy·notification·crypto)·audit·integrations | ✅ |
| workers → core 모듈 service + integration 스키마 | ✅ |

---

## 10. Coding Convention

| 대상 | 규칙 |
|------|------|
| Python | PEP8 + ruff, 타입 힌트 필수, 서비스 메서드는 DTO in/out |
| 테이블 | `{schema}.{snake_case}`, PK `id`, 시각 `*_at`(TIMESTAMPTZ), 날짜 `*_on`/`*_from`/`*_to`(DATE) |
| API | 리소스 복수형 kebab, 액션은 동사 서브경로 (`/appeals/{id}/approve`) |
| TS/React | 컴포넌트 PascalCase, 훅 use-, 폴더 kebab-case |
| 환경 변수 | Plan §7.2 — 시크릿은 .env (저장소 커밋 금지), `JWT_SECRET` 256bit+ 분기 회전 |
| 로그 | 구조화 JSON: `{ts, level, request_id, module, event, ...}` — 워커 잡도 request_id 전파 |
| 에러 | 모듈 예외 → `ERPException(code, message)` → 전역 핸들러가 §4.2 포맷 변환 |

---

## 11. Implementation Guide

### 11.0 구현 기술 결정표 (design-validator H1 반영)

| 결정 | 선택 | 비고 |
|------|------|------|
| 마이그레이션 실행 | **core 컨테이너 entrypoint에서 `alembic upgrade head` 자동 실행** (advisory lock으로 동시 실행 방지). 스키마 9개 생성·`erp_app` GRANT·마약류 REVOKE는 최초 마이그레이션 `0001_init_schemas.py`에 포함 | 수동 개입 없는 배포 |
| Excel 생성 | **openpyxl** (write_only 스트리밍 모드) — 5천 행 이하 동기 응답, 초과 시 비동기 잡 + 알림으로 다운로드 링크 | 500ms 목표는 목록 조회에만 적용, export는 별도 |
| PDF 생성 | **WeasyPrint** (HTML/CSS 템플릿 — Jinja2). 폰트(Noto Sans KR) 이미지에 번들. **§6.7 커스텀 url_fetcher·autoescape 필수** | headless 브라우저 배제 (리소스 상한) |
| 대시보드 위치 | **common 모듈의 BFF 엔드포인트** `GET /dashboard` — 위젯별 타 모듈 `interfaces.py` 호출로 집계 (직접 조인 금지 준수) | 역할별 위젯 구성은 policy 테이블 |
| 워커 스케줄러 | worker-batch 내 APScheduler — 잡: 수가 동기화(일·야간), 면허 만료 알림(일), 근태 집계(일), AI 정합성 대사(주), 백업(일 22:00), 파기 식별(월), 간호등급(G2 후) | 모든 잡 진료시간 외 |
| Redis Streams 소비 | consumer group `erp-workers`, 메시지 ack + 미처리(pending) 재할당 5분 | at-least-once |

### 11.1 모노레포 초기화 + 구현 순서 (Plan §8 M2~M5 매핑)

```
M2 (T+5주): infra(compose·스키마 마이그레이션) → common(인증 FR-01·RBAC·알림·감사)
            → regulatory(fee_master+동기화+공시+AI 발신 골격) ★최우선
M3 (T+7주): hr(직원·면허·휴가·듀티[G3 룰 stub 경고 없이 저장만]·급여·증명서)
            → 그룹웨어 어댑터(Mock 우선)
M4 (T+9주): claims(업로드 fallback·스테이징·이의신청 — G1 파서는 샘플 확보 시)
            → patient(환자·접수·수납·예약·일마감→F1 전표)
M5 (T+10주): scm(품목·FIFO·마약류 append-only·발주) → finance(전표·예산·정산·손실 전표 수용)
```

### 11.2 Gate 해소 시 본 문서 개정 항목

- G1 → §3.3 조정사유코드 마스터 + §5.1 HiraNoticeSource 파서 스펙 추가
- G2 → regulatory.nursing_grade_config 산식 + 일배치 잡 설계 추가
- G3 → hr 듀티 룰 테이블(조항·기산·임계값) 확정 → 경고 활성화
- G4 → DB 상이 시 §3 DDL 방언 전환 노트

---

## 12. 구현 확장 (v0.4 → 현재) — 코드 = 진실, 문서 동기화

> v0.4(2026-06-07) 이후 출시됐으나 본 설계서 §3·§4에 미반영이던 기능군을 카탈로그로 동기화한다.
> **원칙**: 코드가 단일 SoT — 본 절은 마이그레이션·서비스·엔드포인트로 현 구현을 역추적 문서화한다.
> 현황: **마이그 head 0061**, **14 도메인 모듈**(analytics·audit·claims·common·costing·finance·hr·
> integrations·patient·regulatory·scm·tax·workers + app), **~206 엔드포인트**, **테스트 547 그린**, 프로드 LIVE.
> 진짜 미구현은 FR-43(UDI)·FR-44(RFID) 2건뿐 — 둘 다 Phase 2(EMR/하드웨어 의존).

### 12.1 재무회계 심화 (finance — 전표→재무제표→자산→정산→마감)

| 기능군 | 서비스 | 마이그 | 비고 |
|--------|--------|--------|------|
| 재무제표 | `finance/statements.py` | 0010 | 시산표·손익·재무상태표, balanced 자가검증 |
| 고정자산·감가 | `finance/asset_service.py` | 0008·0012 | 정액/정률 월감가·처분손익(4610/5610) |
| 자산 손상·재평가 (K-IFRS) | `asset_service.py` | 0029·0032 | 손상차손(5620)·재평가누계, 누계 멱등(P1-8) |
| 채권/채무·자금 | `finance/settlement_service.py` | 0009 | 미수금(1130)·수금/지급 전표·aging·자금수지 |
| 예산통제 (약정·전용) | finance 예산 | 0023 | encumbrance·budget amendments |
| 기간/연말 마감 | `period_service.py`·`closing_service.py` | 0022·0025 | 월 잠금·결산분개(이익잉여금), closing 전표 손익 제외(P0-6) |
| 리스 (K-IFRS 16) | `finance/lease_service.py` | 0030 | 사용권자산·리스부채 |
| 전자세금계산서 | `finance/tax_invoice_service.py` | 0041 | 수취·대사 |

### 12.2 인사 심화 (hr — 법정처리·P4 인센티브)

| 기능군 | 마이그 | 비고 |
|--------|--------|------|
| 급여 법정처리 (`hr/statutory.py`) | — | 4대보험·퇴직금·연말정산 순수계산, 연도 요율상수 |
| 보수 프로필·수당단가 | 0033·0037 | generate-bulk 급여 |
| 휴직/복직·오프보딩·부양가족 | 0034·0036·0045 | |
| 4대보험 신고추적·법정교육·건강검진 | 0035·0043 | 취득/상실 신고 |
| 퇴직급여충당·연금유형(DB/DC) | 0026·0031 | 충당부채(2120/5320) |
| 의료진 계약·인센티브·의사별수익 (P4) | 0018·0019 | `incentive_*`·`physician_revenue.py`, IDOR 행검증(P0-2) |
| 전자결재 연동(휴가) | 0015 | leave_requests approval_id |

### 12.3 보험심사 자동화 (claims — FR-41 + 워크리스트 + 자동심사)

| 기능군 | 서비스 | 마이그 | 비고 |
|--------|--------|--------|------|
| 사전심사 룰엔진 | `claims/pre_review.py` | 0013 | 11종 룰(중복·산정횟수·미등재·금액이상 등) |
| 워크리스트·케이스 | `worklist_service.py` | 0038·0049·0057 | 케이스·적발·코멘트·상태전이·진료일 부착 |
| Tier2 통계 이상치 | — | 0039 | STAT_OUTLIER(배치 내 코드 percentile) |
| **자동심사 P1/P2 + 실시간** | `worklist_service`·`inbound_consumer` | 0059·0060 | P1 무적발 자동 CLEARED·P2 확정오탐 자동 DISMISS(옵트인)·웹훅 push 실시간(§5.4) |
| 질의/회신 협업 루프 | review_queries | 0051 | P2 삭감방지 |
| 통보서(MEDRLT) 파서 | `ingest_service.py`·`medrlt_parser.py` | 0047 | **G1 게이트 `medrlt_live_enabled`(기본 차단)** — 실물 2건 검증 후 활성 |

### 12.4 원가·세무·경영분석

| 기능군 | 모듈 | 마이그 | 비고 |
|--------|------|--------|------|
| ABC 원가 배부 (FR-42/50) | `costing/abc_service.py`·`allocation.py` | 0014·0024 | 실 인건비·삭감손실 배선, 마감 스냅샷 |
| 세무 (원천징수·4대보험신고·연말정산) | `tax/` | — | 사업장현황·이행상황 |
| 경영 BI | `analytics/` | — | KPI·자금예측·부서손익·추세 |

### 12.5 HIS 연계·진료라인 SoT (integrations·patient·scm·regulatory)

| 기능군 | 위치 | 마이그 | 비고 |
|--------|------|--------|------|
| HIS 웹훅 인박스 | `integrations/his_webhook_router.py`·`inbound_consumer.py` | 0016 | X-Integration-Key·멱등·SKIP LOCKED 동시소비 차단(P1-7) |
| HIS 재고 미러 | — | 0020 | his_stock_mirror |
| 진료라인 미러 (D-6 SoT) | `patient/billing_service.py` | 0040·0042·0050·0055·0058 | HIS 산정가·기관종별 본인부담·자격·상병코드 |
| 약품 코드 매핑 | `regulatory/drug_map_service.py` | 0053 | HIS 비표준코드↔보험코드, reject 감사 |
| 재고 FIFO·실사·위수탁 | scm | 0027·0028·0044 | 단가·감모손실·위수탁계약 |

### 12.6 §5.4 실시간 사전심사 (웹훅 push)

```
HIS 청구(웹훅) → his_inbound_events(인박스) → [Redis signal "erp:inbound:new" → inbound_listener 즉시 소비]
                                          ＋ job_inbound_consume(5분 폴링 안전망)
                                          → [표적 사전심사 sync] → 적발 + P1 자동 이상없음(+P2 자동 오탐)
                                          ＋ job_prereview_sync(새벽2시 당월/전월 백필·룰변경 반영)
```
- `his_webhook_router._signal_inbound()` 커밋 후 Redis publish → `scheduler.inbound_listener()`(상주) 즉시 소비. 폴링은 Redis 장애 안전망.

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-06-07 | Initial draft — Phase 1 시스템 설계 (Gate G1~G4 보류 명시) | weruby |
| 0.2 | 2026-06-07 | design-validator 검증(88점) 13건 반영 — [H] refresh_tokens 테이블, suppliers/po_lines DDL·API, §6.4 저장 암호화 대상 표, §6.5 백업/DR·파기 잡 설계, §11.0 구현 기술 결정표(Alembic 자동 실행·openpyxl·WeasyPrint·BFF 대시보드·APScheduler) [M] request_id 전파 규약, FR-29 근태 집계 테이블·API·화면, ERD via-라벨, §6.6 보안 헤더·CORS, 청구 상태 단일 소스=claims 정리 | weruby |
| 0.3 | 2026-06-07 | **아키텍처·보안 심층 교차 검토 21건 반영** — [아키텍처 C] ① 인터페이스 **세션 전달 규약(UoW)** 신설 — 단일 Tx 보장의 전제 ② fee_master EXCLUDE(GiST) 기간 겹침 방어 + 동기화 멱등키 ③ FIFO FOR UPDATE(SKIP LOCKED 금지)·음수 CHECK ④ 마약류 advisory lock 직렬화·정정 규칙·balance 시스템 산출 ⑤ FR-54 마감 후 정정(역분개) 경로 ⑥ access_log 비동기 기록 ⑦ Streams 멱등키 ⑧ payroll JSONB→라인 테이블 ⑨ G5(기한 산정 법적 성격) Gate 신설. [보안 C] GCM 저장 포맷(nonce·AAD·키 회전 절차), phone blind index, WeasyPrint url_fetcher(LFI/SSRF 차단). [보안 H] token_version Redis 캐시, refresh 원자 CAS+grace, caller_scope 필수 인자(행 필터 우회 차단), FR-53 전처리·content_hash 바인딩·첨부 제외, export 다운로드 인가(IDOR), 업로드 검증(§6.7), reprocess 권한·멱등, CSP 구체화, 감사 테이블 DB REVOKE | weruby |
| 0.4 | 2026-06-07 | **Gap 분석(87.2%) 동기화** — 역방향 Gap 반영: 부서 CRUD·export·movements 분리·leaves/remaining·budgets/execution 엔드포인트 추가, claim_batches 컬럼(decided_on·notice_arrived_on·staging_id), VoucherService 시그니처(voucher_on·created_by), §9.1/9.2 구조 개정(service 공개 클래스 = cross-import 단위, Protocol은 integrations/ 집중) | weruby |
| 0.5 | 2026-06-15 | **§12 구현 확장 동기화** — gap-detector 재측정(FR 충족 ~98% / 문서 정합 ~55%)의 역방향 드리프트 해소. v0.4 이후 출시된 ~10개 기능군(재무 자산·손상·재평가·리스·정산·마감, 인사 P4 인센티브·퇴직충당·휴직·4대보험, claims 사전심사 자동화 P1/P2·실시간, 원가 ABC, 세무, BI, HIS 인박스·진료라인 SoT, 약품매핑)을 마이그(0007~0061)·서비스·EP로 카탈로그화. §5.4 실시간 사전심사·FR-38 G1 게이트(medrlt_live_enabled) 명문화. 14모듈·~206EP·테스트 547 현황 반영 | Claude Code |
