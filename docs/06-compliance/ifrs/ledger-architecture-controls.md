# IFRS 원장 아키텍처 · 내부통제 · 스키마 설계원칙 점검서

> 범위: 회계 원장(GL) 아키텍처 · Append-only 불변원장 · 감사/감사추적 · 직무분리(SoD) · 승인 워크플로 · 마감/잠금/재개 이력 · XBRL 태깅 · 스키마 설계원칙(불변원장+이벤트+다중원장차원) · 권한모델.
> 작성: 2026-07-14. 근거: `services/core/src/erp` 코드 대조(file:line 명시).
> 준거: K-IFRS · K-ISA · ICFR/SOX 내부통제 관점 · 의료기관 회계기준(병행).
> **본 문서는 기존 IFRS 부합도(측정·표시·공시 ~92%) 트랙**(`docs/06-compliance/ifrs/` methodology/controls/status-register)**과 다른 축** — 원장 데이터 아키텍처와 통제(무결성·추적성·직무분리)를 다룬다.

---

## 0. 요약 (Executive Summary)

현행 회계 엔진은 **단일 GL·복식부기·역분개 기반 불변성·source_ref 멱등·잔액 도출식**이라는 견고한 골격을 갖췄고, 측정엔진(ECL/리스/자산/이연법인세/DBO/외화 등)과 결산조정이 모두 단일 전표 경로(`VoucherService.create`)로 귀결된다("별도 장부 금지" — `finance/disclosure_service.py:181`). 그러나 **통제의 강제 지점이 대부분 애플리케이션 레벨**에 있어, IFRS/SOX 감사 관점의 4대 취약이 존재한다:

| # | 취약 | 심각도 |
|---|---|---|
| G1 | 재무 원장(`finance.vouchers`/`voucher_lines`)에 **DB 레벨 불변성(REVOKE/트리거) 부재** — audit_log·narcotics_ledger만 강제 | **HIGH** |
| G2 | **기간 마감/재개 직무분리(SoD) 부재** — 동일 ADMIN이 close→reopen→re-close, 재개 시 `closed_by/at` NULL화, ClosingSnapshot 무버전 덮어쓰기 | **HIGH** |
| G10 | **XBRL/iXBRL/전자공시(DART) 태깅 전무** — 규제 제출 요구 시 net-new | **HIGH(정보)** |
| G11 | 암호학적 tamper-evidence 부재 — 상위 DB 권한(DBA/owner)의 원장 변조를 탐지할 수단 없음 | **MED-HIGH** |

전반 평가: **거래 기록·정합·멱등은 우수(A), 무결성 강제·마감통제·추적 완결성은 보완 필요(C+)**. 아래 갭 등록부(§5)와 대응 시나리오(§6), 스키마 설계원칙(§7), 로드맵(§8) 참조.

---

## 1. 현행 원장 아키텍처 (구조 점검)

### 1.1 코어 데이터 모델 (`finance/models.py`)

- **`Account`**(`:13-25`, `finance.accounts`) — 계정과목. `code`(UNIQUE), `kind`(REVENUE/EXPENSE/ASSET/LIABILITY/CAPITAL), `parent_id`(트리), `active`(비활성화≠삭제), `cost_type`(FIXED/VARIABLE), `cost_function`(COGS/SGA/OTHER·IAS 1 기능별). **잔액 컬럼 없음**.
- **`Voucher`**(`:28-58`, `finance.vouchers`) — 전표 헤더. 멱등키 `UniqueConstraint(source_module, source_ref)`(`:32`), 상태 CHECK `DRAFT|PENDING|POSTED|REJECTED|REVERSED`(`:34`). `voucher_on`, `source_module/source_ref`(FK 아님), `status`(기본 POSTED), 역분개 링크 `reversed_by`/`reverses`, maker-checker(`submitted_by/at`·`approved_by/at`·`reject_reason`), `draft_lines` JSON(승인 전 미전기 제안).
- **`VoucherLine`**(`:61-77`, `finance.voucher_lines`) — 분개 라인 = **GL 그 자체**(별도 GLEntry/Ledger 테이블 없음). `voucher_id` FK, `account_code`(**문자열·accounts FK 아님**), `side` CHECK(DEBIT/CREDIT), `amount` Numeric(14,0)(원화 정수·부호 없는 단일 금액+side), `department_id`(nullable), `detail`.

### 1.2 단일 전기 창구·멱등·정합

- **단일 choke-point**: 모든 postings(측정엔진·서브원장·HIS 이벤트)가 `VoucherService.create()`(`finance/service.py:25`)를 경유. Decimal 정확비교로 차대변 균형 강제(`:44-49`).
- **멱등**: `(source_module, source_ref)` UNIQUE + 앱단 단락(존재 시 기존 전표 반환, `service.py:30-35`) → 중복 메시지/재처리 안전.
- **잔액 도출(파생)**: 저장 잔액 없음. `SUM(CASE side...)`(`statements.py:14-17`)로 `POSTED+REVERSED` 라인을 합산(`_by_account` `:49-64`) — 역분개가 원분개를 상쇄하므로 상태 변경 없이 잔액 정정. DRAFT/PENDING은 라인 미생성(`draft_lines` JSON만)이라 집계에서 자연 제외.

### 1.3 불변성(역분개) — 애플리케이션 레벨

- 정정은 **역분개(reversal)**로만: `VoucherService.reverse`(`service.py:142-168`)가 원전표 라인의 side를 반대로 새 전표 생성(`source_ref=".../reversal-{id}"`, 당일자), `reverses`/`reversed_by` 링크, 원전표는 `status=REVERSED`만 변경. **금액·라인은 절대 UPDATE/DELETE 안 함**(grep 확인). 역분개는 현 개방기간에 계상.

**구조 평가**: 복식부기·멱등·역분개·잔액도출은 IFRS/감사 관점 정석. 단, 불변성이 **코드 규율에만 의존**(§2.1 G1).

---

## 2. 통제 · 데이터모델 점검

### 2.1 Append-only 불변원장 기준

| 대상 | DB 레벨 append-only | 근거 |
|---|---|---|
| `audit.audit_log` | ✅ `REVOKE UPDATE, DELETE FROM erp_app` | `0001_init_schemas.py:171-172` |
| `audit.access_log` | ✅ 동일 | `0001:171-172` |
| `scm.narcotics_ledger` | ✅ + **해시체인**(prev_hash) | `0122_narcotics_entry_on.py` |
| **`finance.vouchers`** | ❌ **없음** | — |
| **`finance.voucher_lines`** | ❌ **없음** | — |

**[G1·HIGH]** 재무 원장은 트리거·CHECK·REVOKE 어느 것도 없어, ORM/DB가 posted 라인의 UPDATE/DELETE를 그대로 수용한다. 불변성은 "모든 쓰기가 `VoucherService`를 경유한다"는 규율에만 의존. audit_log·narcotics는 DB가 강제하는데 정작 **재무 원장은 미보호** — 가장 큰 무결성 취약.
**[G14·LOW]** REVOKE는 `DO $$ ... IF role exists`라 `erp_app` 미프로비저닝 환경에서 조용히 skip. access_log **2년 법정 보존 floor 미강제**(만료 purge 잡 부재).

### 2.2 감사 · 감사추적

- **`AuditLog`**(`audit/models.py:10-22`) — actor·action·entity·target_id·**before/after JSONB**·at. `AuditService.record_audit`(`audit/service.py:44-53`)로 업무 트랜잭션과 원자적 기록.
- **커버리지 = 부분**: 재무는 라우터 레이어 `_money_audit`(`finance/router.py:28-31`)가 전표 생애주기 EP(draft/submit/approve/reject/reverse) 감사. **그러나 코어 `VoucherService`는 내부에서 record_audit 미호출** → **시스템/자동 전표(감가·마감·claims/patient sync·SCM)는 AuditLog 미기록**.
  - **[G3·MED]** 자동 전표는 `created_by` null + AuditLog 없음 → source_ref로 "출처 시스템"만 식별, **책임 개인·before/after 부재**.
  - **[G16·LOW]** `voucher_lines`에 actor/timestamp 없음(헤더 귀속만).
- **접속기록**(`AccessLog`·법정) — Redis stream 버퍼 + INSERT 폴백(유실금지), `job_access_log_flush`(`scheduler.py:71-92`) **delete-after-commit 불변식**(커밋 후에만 xdel·`scheduler.py:82-86`)로 no-loss 보장. 착지 후 REVOKE 보호.
- **이상탐지 리뷰**(감사·이상탐지) — Benford/중복/비정상(`analytics/audit_service.py`)은 후보 제안·자동판정 안 함. 사인오프는 `finance.audit_reviews`(`finance/models.py:654-679`·reviewed_by/at·disposition).
  - **[G4·MED]** 리뷰 테이블은 finance 스키마(full grant)라 **가변·삭제 가능**(`audit_review_service.py:43-50` hard delete, `analytics/router.py:224-232`), upsert/delete가 **AuditLog 미기록** → 사인오프 자체가 tamper-evident 아님.

### 2.3 직무분리(SoD)

5개 지점에서 강제되나 **불균일**:

| 통제 | 위치 | 기제 |
|---|---|---|
| 결재엔진(PO/계약/폐기) | `approval_engine.py:114-115` | `created_by==actor` 차단(**override 하에서도 유지**) |
| GL 전표 전기 | `finance/service.py:112-113` | `actor in (created_by, submitted_by)` 차단(작성·상신 모두) |
| 일마감(원무) | `patient/service.py:155-176` | 당일 PAID 100% 본인입력이면 self-close 차단 |
| PO/계약 레거시 폴백 | `po_service.py:101-103` 등 | 요청자 본인 승인 차단 |

- **[G2·HIGH] 기간 마감/재개는 SoD 없음.** `period_service.close`(`:128-158`)·`reopen`(`:160-170`)이 단일 `user_id`, 작성자≠승인자 체크 없음 → **동일 ADMIN이 마감→재개→재마감** 가능(재개는 `closed_by/at` NULL화·note 덮어씀). 4-eyes 없음.
- **[G12·LOW]** 일마감 self-close는 `force=true`로 동일 캐셔 우회 가능(감사만·차단X).

### 2.4 승인 워크플로 (ApprovalEngine)

- **`common/approval_engine.py`** — `ApprovalRule`(min/max_amount·category·steps JSON)·`ApprovalInstance`(status PENDING/APPROVED/REJECTED)·`ApprovalStep`(step_no·approver_type ROLE/DEPT_HEAD/USER·acted_by/at). 문서유형 `{PO, OUTSOURCING_CONTRACT, MEDICAL_CONTRACT, ITAM_DISPOSAL}`.
- 금액밴드 라우팅(`find_rule` narrowest band), 멱등 build, **행잠금 `with_for_update`**(`:109-111` 동시 이중승인 직렬화), CEO override(DIRECTOR·SoD 유지·remaining SKIPPED), 모든 액션 immutable audit(`record_audit`). ITAM 폐기 임계는 **서버측 장부가 도출**(클라 우회 방지·`itam/service.py:639-650`) — 강한 통제.
- **[G13·LOW/MED] GL 전표 전기는 ApprovalEngine과 분리**된 단순 상태머신(draft→submit→approve). **금액 임계 다단계 라우팅 없음** — 거액 수기분개도 1인 2차승인만. `approve`는 `session.get`만(`:107`·행잠금 없음, PENDING 가드로 완화).

### 2.5 마감/잠금/재개 이력

- **`FiscalPeriod`**(`finance/models.py:199-216`) — yyyymm(PK)·status(OPEN/CLOSED)·마감스냅샷(revenue/expense/net_income/voucher_count)·`closed_by`/`closed_at`/`note`. 확정 재무제표는 별도 `ClosingSnapshot`(`:356-361`·UNIQUE(yyyymm,kind)).
- **잠금 강제 = GL 쓰기경로 단일 지점**: `create`(`service.py:37-41`)·`approve`(`:114-117`)에서 CLOSED 기간 전기 차단. 대부분 서브원장이 create를 경유하므로 상속.
- **[G5·MED]** `allow_closed=True`(`service.py:28,39`)는 **미감사 서비스 백도어**(연말결산 `closing_service.py:75` 사용).
- **[G6·MED]** 서브원장(AR/AP/DepreciationEntry/stock/TaxInvoice) **직접 쓰기는 FiscalPeriod 체크 없음** → 동기 전기 없는 서브원장 변경은 마감기간에도 가능(GL 스냅샷과 드리프트).
- **[G2 관련]** 재개 감사(`PERIOD_REOPEN`)·사유필수는 **라우터에서만**(`router.py:1217-1221`) — 서비스 직접 호출은 무추적. 재개가 `closed_by/at` NULL화·note 덮어씀으로 **온레코드 마감귀속 파괴**. `ClosingSnapshot`은 재개 시 무효화 안 됨 → 재마감이 확정 재무제표를 **무버전 덮어씀**.
- **[G15·MED]** BI `_is_closed_month`(`analytics/marts.py:36-42`)는 "달력월 < 현 KST월"로만 판정 — `FiscalPeriod.status`와 **완전 분리**. BI freeze와 실제 회계마감 상태 불일치 가능.

### 2.6 XBRL 태깅

**[G10·HIGH(정보)] 전무 확인.** 전체 리포지토리(`*.py/ts/tsx/md/json`)에서 `xbrl|ixbrl|taxonomy` 유효 히트 0(유일 히트는 직종 taxonomy·무관). iXBRL·element 태깅·DART/EDGAR 전자공시 생성기 없음. 재무제표 출력은 순수 JSON + 일부 `.xlsx`. **K-IFRS 전자공시(DART) 제출 요구 시 net-new 레이어** 필요.

---

## 3. IFRS 측정엔진 · 다중원장/차원 · 이벤트 (참고 — 기존 트랙)

- **측정엔진**(preview/post·delta·멱등): ECL 1109(`allowance_service`)·리스 1116(`lease_service`)·자산 IAS16/1036(`asset_service`·`_depreciate_through` catch-up)·이연법인세 1012(`deferred_tax_service`·P/L origin만·OCI 역추적 제외)·변동대가 1115(`claims/variable_consideration_service`)·미청구수익 1115(`revenue_recognition_service`·수기금액)·DBO 1019+OCI(`hr/retirement_service`)·외화 1021(`forex_service`)·무형/차입원가 1038/1023(`intangible_service`). 상세=`docs/06-compliance/ifrs/methodology/`.
- **재무제표**(`statements.py`): 전부 VoucherLine 집계 파생. TB·손익(기능별 IAS1)·의료기관 운영성과표·재무상태표·포괄손익·자본변동·현금흐름(직접+간접)·비율·중간보고·부문(analytics). **IFRS 분류는 `disclosure_service` STD_ACCOUNTS/CONFORMANCE_CATALOG + `statements.py` 하드코딩 prefix/set** — 기계판독 taxonomy 없음(**[G8·MED]** 계정마스터 미저장·추적성 낮음).
- **다중원장/차원**: **단일 GL·다중장부 없음**(법정/관리/세무 별도 book 없음·이연법인세도 동일 GL 혼재). 라인 차원 = `department_id` 단일. 외화=별도 GL 계정(`CorporateAccount.gl_account_code`)·라인은 원화정수(원통화/환율 필드 없음). **[G9·MED]** cost center/segment/project/currency 라인 차원 부재 → IFRS 8 부문은 department+costing 간접도출.
- **이벤트**: **event-sourcing 아님.** HIS `HisInboundEvent`(`integration.his_inbound_events`)는 **멱등 staging 인박스**(source_ref UNIQUE·NEW/APPLIED/FAILED/DEAD), `inbound_consumer`가 도메인 핸들러로 **즉시 직접 GL 전기**. 이벤트는 consumed-discarded(APPLIED)·재생/투영으로 원장 재구성 불가.

---

## 4. 권한모델 검증 (`common/policy.py`)

- **모델**: 역할 7종(DIRECTOR/ADMIN/MANAGER/STAFF + 도메인관리자 IT/FINANCE/LOGISTICS + WORKSTATION), ScopeKind(ALL/DEPT/SELF/NONE), POLICY dict `(resource,action)→{Role:Scope}`, **default-deny**(`resolve_scope`·미등록=NONE), `require_scope`로 강제.
- **SoD 설계 양호점**: `_FIN_WRITE`가 FINANCE_MANAGER에서 **`finance.periods`·`finance.settlement` 배제**(`policy.py:169-174`) — GL 준비 vs 기간잠금/자금전기 분리. `_SCM_WRITE`가 LOGISTICS_MANAGER 마약류 write 배제.
- **[G-ADMIN·MED] ADMIN super-role**: vouchers·periods·settlement·closing·accounts·budgets·users 전권. 단일 전표의 자기승인만 `service.py:112` 2인 강제로 막힘. 기간 마감/재개는 SoD 부재(G2)라 ADMIN 1인 완결 가능. **역할 내 분리(예: 마감승인자 별도) 없음**.
- 검증 결론: **정책 프레임(default-deny·도메인관리자 최소권한·재무준비/마감 분리)은 견고**. 보완 필요 = ①기간 마감/재개 SoD ②ADMIN 세분(마감·재개 별도 권한키) ③거액 분개 다단계.

---

## 5. 갭 등록부 (Gap Register)

| ID | 갭 | 심각도 | 근거 |
|---|---|---|---|
| G1 | 재무 원장 DB 레벨 불변성 부재 | HIGH | `0001:171-172`(audit만)·finance 미보호 |
| G2 | 기간 마감/재개 SoD 부재·재개가 마감귀속 파괴·스냅샷 무버전 덮어씀 | HIGH | `period_service.py:160-170` |
| G10 | XBRL/전자공시 태깅 전무 | HIGH(정보) | 전수 grep 0 |
| G11 | 암호학적 tamper-evidence 부재(DBA 변조 무탐지) | MED-HIGH | narcotics만 해시체인 |
| G3 | 시스템 자동 전표 감사귀속 부재(AuditLog·created_by) | MED | `service.py:26-59` |
| G4 | 이상탐지 리뷰 사인오프 가변/삭제/미감사 | MED | `audit_review_service.py:43-50` |
| G5 | `allow_closed=True` 미감사 백도어 | MED | `service.py:39` |
| G6 | 서브원장 독립 기간잠금 부재 | MED | FiscalPeriod 참조 finance만 |
| G8 | IFRS 분류 코드 하드코딩(계정마스터 미저장) | MED | `statements.py:21-45` |
| G9 | 원장 차원 빈약(department 단일·통화/부문/프로젝트 없음) | MED | `models.py:76` |
| G15 | `_is_closed_month` ↔ FiscalPeriod.status 분리 | MED | `marts.py:36-42` |
| G7 | `account_code` FK 아님(문자열) | MED | `models.py:73` |
| G12 | 일마감 self-close force 우회 | LOW | `patient/service.py:172` |
| G13 | GL 전표 approve 행잠금 미적용·거액 다단계 없음 | LOW-MED | `service.py:107` |
| G14 | REVOKE 조건부 skip·access_log 보존 floor 미강제 | LOW | `0001` DO 블록 |
| G16 | voucher_lines actor/timestamp 없음 | LOW | `models.py:61-77` |

---

## 6. 대응 시나리오 (Response Scenarios)

**S1 — 외부감사 "원장 무결성 입증" 질의**
- 현답변: 복식부기 균형 상시 자가검증·역분개 규율·source_ref 멱등·POSTED/REVERSED 도출. audit_log/access_log는 DB append-only.
- 한계·리스크: 재무 원장 자체는 DB 강제 없음(G1)·DBA 변조 무탐지(G11)·자동전표 무귀속(G3). ⇒ **보완 전이면 "애플리케이션 통제 의존" 명시**, 보완 후 "DB 강제 + 해시체인" 답변 가능.

**S2 — 마감 후 부정 재개·조정 (마감통제 부정)**
- 현상태: 동일 ADMIN이 마감→재개→수정→재마감, closed_by 흔적 NULL화, 확정 스냅샷 무버전 덮어씀(G2). audit_log의 PERIOD_REOPEN만 흔적.
- 대응 보완: 재개 SoD(요청자≠승인자)·재개 사유·직전 스냅샷 버전 보존·재개를 서비스단에서도 audit 강제.

**S3 — "이 전표를 누가 만들었나" (자동전표 추적)**
- 현상태: 수기전표는 maker-checker+money-audit로 추적. **자동전표(감가·마감·sync)는 created_by null·AuditLog 없음**(G3) → source_ref로 출처 배치만.
- 대응: 시스템 액터 상수화(예: `SYSTEM`/배치 job id) + `VoucherService.create` 내부 audit 훅.

**S4 — DART/금융감독 전자공시(XBRL) 제출 요구**
- 현상태: 태깅 전무(G10). 재무제표 JSON/xlsx만.
- 대응: taxonomy 매핑 레이어(계정↔IFRS element) + iXBRL 생성기 = **별도 프로젝트**(사전 스코핑 필요). 계정마스터에 IFRS element 컬럼(G8 해소와 결합) 선행 권장.

**S5 — DBA/상위권한 원장 변조 의심**
- 현상태: REVOKE는 erp_app만 대상·상위권한 우회 가능·탐지수단 없음(G11).
- 대응: 재무 원장/감사로그 해시체인(narcotics 패턴 확대) 또는 외부 append-only 저장(WORM/객체락)·정기 무결성 검증 잡.

**S6 — 마감월 서브원장 사후 변경 (드리프트)**
- 현상태: 서브원장 직접 쓰기는 마감가드 미적용(G6)·BI freeze는 status 무관(G15).
- 대응: 서브원장 쓰기에 기간잠금 가드 삽입 or GL 스냅샷 대사 잡·`_is_closed_month`를 FiscalPeriod.status 연동.

---

## 7. IFRS 스키마 설계원칙 (검증 + 보완)

### 7.1 불변원장 (Immutable Ledger)
- **원칙**: posted 원장 라인은 **불변** — 정정은 역분개, DB가 UPDATE/DELETE를 물리 차단, 상위권한 변조는 암호학적으로 탐지 가능.
- **현행**: 역분개 규율·도출잔액 ✅ / **DB 강제·해시체인 ❌**(G1·G11).
- **보완(P0)**: `finance.vouchers`·`voucher_lines`에 (a) `REVOKE UPDATE, DELETE FROM erp_app`(마감/역분개 경로는 상태·링크 컬럼만 갱신 필요 → **부분 허용 트리거** 또는 상태전이 전용 컬럼/보조테이블 설계), (b) 선택적 해시체인(prev_hash·narcotics 패턴).
  > ⚠ 단순 REVOKE는 `reverse`의 `original.status=REVERSED` 갱신·마감스냅샷 저장과 충돌 → **트리거 기반 "append + 상태전이 화이트리스트"** 설계 필요(단순 코드픽스 아님·마이그+검증 요함).

### 7.2 이벤트 (Events)
- **원칙**: 외부/도메인 이벤트는 **멱등·불변 인박스**에 먼저 착지, 원장은 그로부터 파생(재생 가능). 이벤트 원본은 보존.
- **현행**: HIS 인박스는 멱등 staging(✅) 이나 **consumed-discarded**(재생/투영 불가) — event-sourcing 아님.
- **보완(P2·선택)**: 인박스 이벤트를 APPLIED 후에도 **보존(retention)** + `voucher.source_ref`↔이벤트 역참조로 "이벤트→전표" 계보 조회. 완전 event-sourcing(원장 재구성)은 과설계 — **계보 추적성**까지가 실효 목표.

### 7.3 다중원장 차원 (Multi-Ledger / Dimensions)
- **원칙**: 라인은 계정 외 **부문·코스트센터·프로젝트·통화** 차원 태깅, 필요 시 **다중장부(법정/관리/세무) book 차원**.
- **현행**: 단일 GL·`department_id` 단일·외화는 별도 GL 계정.
- **보완(P1)**: (a) `voucher_lines`에 확장 차원(`cost_center`/`project`/`segment` nullable·또는 JSONB `dims`) 추가 — IFRS 8 부문·관리회계 정밀화. (b) 통화 라인화(`currency`/`fx_rate`/`amount_ccy`) — 외화 정밀. (c) 다중장부는 **book 태그**(statutory/mgmt/tax)로 경량 도입 검토("별도 장부 금지" 원칙과의 정합은 회계정책 결정 필요).

### 7.4 권한모델 (RBAC)
- **원칙**: default-deny·최소권한·재무 준비/승인/마감/자금 분리.
- **현행**: 프레임 견고(FINANCE_MANAGER의 periods/settlement 배제) / ADMIN super-role·마감 SoD 미비.
- **보완(P0/P1)**: (a) `finance.periods:approve` 별도 스코프 신설 → 마감/재개를 준비자와 분리. (b) ADMIN 세분(마감·재개 전용 승인역). (c) 거액 분개 ApprovalEngine 라우팅(임계 초과 시 다단계).

---

## 8-0. 이행 현황 (Remediation Status)

- **P0 완료(v1.100.0·마이그0191)**: G2 마감/재개 SoD(마감귀속 보존·마감자≠재개자·스냅샷 버전보존)·G3+G5 자동전표 감사귀속(`VoucherService.create`→audit_log)·G4 사인오프 tamper-evidence.
- **P1-a 완료(v1.101.0·마이그0192)**: **G1 재무원장 DB 레벨 불변성** — `finance.voucher_lines` REVOKE UPDATE/DELETE(라인=INSERT 전용) + `finance.vouchers` BEFORE UPDATE/DELETE 트리거(삭제 차단·불변컬럼 voucher_on/source_*/created_by/created_at 변경 차단·상태/maker-checker/역분개 컬럼만 허용). 트리거는 상위권한(owner)에도 적용. **잔여 G11(암호학적 해시체인·DBA 변조 탐지)은 후속** — REVOKE+트리거는 erp_app 및 일반 UPDATE 경로를 봉쇄하나, superuser의 트리거 우회(`session_replication_role`)까지는 미탐지.
- **P1-b 완료(v1.102.0·마이그불변)**: **G15 BI freeze ↔ 회계마감 정합** — `_is_closed_month`가 "달력월<당월"만 보던 것을 `FiscalPeriod.status`(CLOSED만 동결·OPEN/재개는 라이브) 우선으로, 기록 없으면 달력 프록시 폴백. BI 동결과 실제 회계마감 상태의 괴리 해소.
- **P1-c 완료(v1.103.0·마이그불변)**: **G6 마감 무결성 대사(S6)** — 서브원장 쓰기를 일괄 차단(late-period 업무 판단 필요·오탐 위험)하는 대신, 비침습 **대사 컨트롤**로 접근. `PeriodService.reconcile`이 확정 `ClosingSnapshot`(활성 버전) 대비 현재 재도출 재무제표(TB 차대·순이익·자산/부채/자본)의 델타를 산출·`GET /finance/periods/{yyyymm}/reconcile`(finance.periods read)·finance-periods "대사" 버튼. P1-a로 마감월 GL이 불변이라 정상 시 드리프트 0, allow_closed 사후전기·통제우회는 델타로 노출.
- **G11 완료(v1.104.0·마이그0193)**: **원장 무결성 해시 체인** — `finance.ledger_integrity`(append-only·REVOKE) 체크포인트 테이블 + `LedgerIntegrityService`(voucher_lines 증분 세그먼트 SHA-256 체인·저비용 배치) + `verify`(재계산 대조). 일 1회 `job_ledger_integrity`(체크포인트+검증·변조 시 ADMIN 알림)·`GET /finance/ledger-integrity/verify`·`POST .../checkpoint`·finance-periods "원장 무결성 검증" 버튼. **P1-a 트리거를 우회하는 superuser 변조**(`session_replication_role`·직접 UPDATE/DELETE)까지 사후 탐지 → tamper-evidence 완성.
- **마감 권한키 분리 완료(v1.109.0)**: reopen을 `finance.periods:approve`(DIRECTOR+ADMIN)로 분리 — close(write·ADMIN)와 구분해 "확정기간 잠금해제"를 상위 권한으로. P0 자체재개 SoD(마감자≠재개자)와 방어 심화(DIRECTOR가 재개 승인 가능·마감은 ADMIN). RBAC 스냅샷 갱신.
- **거액 전표 상위승인 완료(v1.110.0)**: 수기 전표 approve에 materiality 게이트 — 차변 합계 ≥ `LARGE_VOUCHER_THRESHOLD`(5천만원) 시 FINANCE_MANAGER 승인 차단·ADMIN/DIRECTOR만 승인. `approve(actor_role=)` 서비스 체크(actor_role None인 시스템 경로는 미적용). 향후 임계 설정화·완전 다단계(ApprovalEngine DEPT_HEAD→DIRECTOR 체인)는 심화 옵션.
- **완전 다단계 결재선 완료(v1.112.0·opt-in)**: 수기 전표 approve/reject를 ApprovalEngine(`doc_type=VOUCHER`)에 통합 — **VOUCHER 결재룰 있으면 다단계**(현 단계 승인·완주 시 전기·미완주 PENDING 유지·DIRECTOR override), **룰 없으면 레거시 단일승인 + materiality 게이트**(기본 무변경·backward-compatible). approval_router `_DOC_TYPES`에 VOUCHER 추가(룰 CRUD)·통합 결재함(/approvals)에 "회계 전표" 노출. 엔진의 행잠금·직무분리·감사 재사용.
- **P2 잔여**: G10 XBRL(규제확인 게이트)·이벤트 계보.

## 8. 보완 로드맵 (권고)

| 단계 | 항목 | 갭 | 성격 |
|---|---|---|---|
| **P0** | 기간 마감/재개 SoD + 재개 스냅샷 버전보존 + 서비스단 audit 강제 | G2 | 통제·저위험(로직) |
| **P0** | 자동 전표 시스템 액터 귀속 + `VoucherService` 내부 audit 훅 | G3 | 추적·저위험 |
| **P0** | `allow_closed` 사용 audit + 감사리뷰 사인오프 immutable(AuditLog+삭제차단) | G4·G5 | 통제·저위험 |
| **P1** | 재무 원장 DB 레벨 불변성(트리거 화이트리스트) + 선택 해시체인 | G1·G11 | 무결성·**중위험**(마이그·검증) |
| **P1** | 서브원장 기간잠금 가드 + `_is_closed_month`↔status 연동 | G6·G15 | 통제 |
| **P1** | 라인 확장 차원(cost_center/project/통화) + 계정마스터 IFRS element 컬럼 | G8·G9 | 스키마 |
| **P1** | 마감 SoD 권한키 분리(`periods:approve`)·거액 분개 다단계 | RBAC | 통제 |
| **P2** | XBRL/iXBRL taxonomy 매핑 + 전자공시 생성기(별도 스코핑) | G10 | 신규 프로젝트 |
| **P2** | 이벤트 인박스 보존·전표 계보 역참조 | 이벤트 | 추적 |

> **주의(설계결정 필요)**: P1 원장 DB 불변성(G1)은 `reverse`/마감스냅샷의 상태전이 갱신과 충돌하므로 단순 REVOKE 불가 — **상태전이 화이트리스트 트리거** 또는 **상태를 보조테이블로 분리**하는 스키마 설계가 선행. 사용자 승인 후 별도 마이그레이션 트랙으로 진행 권장.

---

## 부록 A — 근거 파일 인덱스
`finance/models.py`(원장 모델) · `finance/service.py`(전기·역분개·마감가드·SoD) · `finance/statements.py`(도출·분류) · `finance/period_service.py`(마감/재개) · `finance/closing_service.py`(연말) · `common/approval_engine.py`(결재) · `common/policy.py`(RBAC) · `audit/models.py`·`audit/service.py`(감사·접속) · `analytics/audit_service.py`·`analytics/audit_review_service.py`(이상탐지) · `analytics/marts.py`(_is_closed_month) · `integrations/models.py`·`integrations/inbound_consumer.py`(HIS 이벤트) · `alembic/versions/0001_init_schemas.py:171-172`(REVOKE) · `0122_narcotics_entry_on.py`(해시체인).
