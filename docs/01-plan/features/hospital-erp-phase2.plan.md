# hospital-erp Phase 2 도메인 기획서

> **Summary**: Hospital ERP Phase 2 — 사전심사 룰엔진·ABC 원가 정밀화·UDI 역추적·바코드/RFID 스캔 빌링 4개 기능에 대한 도메인 협의 선행 설계 계획. "지금 확정 가능한 골격"과 "협의로 채울 부분"을 명확히 분리하여 협의 게이트를 정의함.
>
> **Project**: hospital-erp
> **Version**: 0.1.0
> **Author**: weruby
> **Date**: 2026-06-08
> **Status**: Draft — 도메인 협의 착수 전 설계 게이트 정의 단계
> **Related**: `hospital-erp.plan.md` v1.2 (Phase 1 계획 — Approved), `hospital-erp.prd.md` (PRD)

---

## 1. 개요

### 1.1 Phase 2 위치

Phase 1(PR #1~#30, 테스트 276건) 완료 시점에서 회계 전표→재무제표→고정자산→채권채무→세무→경영분석, 구매 Procure-to-Pay 전 사이클이 연결된 상태다. Phase 2는 이 위에 **외부 도메인 지식이 필요한 4개 기능**을 추가하는 단계다.

4개 기능의 공통 특성: 구현 자체는 가능하나 **도메인 데이터(심평원 룰셋, 활동 정의, UDI 구조, 하드웨어 사양)가 협의로 결정되기 전에는 핵심 값이 비어 있다.** 이 기획서의 목적은 다음 두 가지를 분리하여 협의 대기 시간을 낭비 없이 병행 진행할 수 있도록 하는 것이다.

| 지금 확정 | 협의 후 확정 |
|-----------|-------------|
| 데이터 모델 골격, API 계약, 평가 엔진 구조 | 룰셋 값, 활동 정의, 원가동인 기준, HIS 연동 포인트 |

### 1.2 Phase 2 대상 기능 및 우선순위 요약

| 순위 | FR | 기능명 | 우선순위 근거 |
|:---:|------|--------|-------------|
| 1 | FR-41 | 사전심사 룰엔진 | 직접 ROI — 삭감 예방은 즉시 수익 보전. 내부 데이터(청구건)로 즉시 작동 가능 |
| 2 | FR-42/50 | ABC 원가 정밀화 | 내부 데이터 자급 — 외부 하드웨어·HIS 연동 없이 인건비·재료비·간접비로 구현 가능 |
| 3 | FR-43 | UDI 역추적 | 환자 안전 핵심이나 HIS EMR 연동이 선행 조건 — 연동 준비 기간이 필요 |
| 4 | FR-44 | 바코드/RFID 스캔 빌링 | 하드웨어 조달·설치·HIS 빌링 API 등 외부 의존성 가장 큼 |

### 1.3 관련 문서

- Plan v1.2: `docs/01-plan/features/hospital-erp.plan.md` — FR 번호 체계, 모듈 구성, 아키텍처 기준
- PRD: `docs/01-plan/features/hospital-erp.prd.md` — 의료기관 특화 요구사항 원문
- 심평원 EDI 규격 참고: `docs/01-plan/references/hira-edi-spec/` (통보서 규격 — 룰엔진 참고)
- HIS 연계: `docs/01-plan/references/hira-review-notice-reference.md`

---

## 2. 우선순위 상세 근거 (MoSCoW)

### Must (현 Phase 2 필수)

**FR-41 사전심사 룰엔진** — 삭감은 이미 발생하고 있는 손실이다. Phase 1 이후 이의신청 워크플로우(FR-40)가 후처리를 담당하지만, 청구 전 차단이 훨씬 효과적이다. 병원 매출의 상당 부분이 보험 청구에서 나오므로 삭감율 1%p 감소도 직접적인 현금 흐름 개선이다. 또한 내부 청구 데이터만으로 작동하여 외부 의존성이 가장 낮다.

**FR-42/50 ABC 원가 정밀화** — FR-50 1차(진료과별 인건비·삭감)가 이미 완료되어 골격이 존재한다. 재료비 배부와 활동 차원 추가는 기존 코드 확장이므로 공수 대비 효과가 크다. 원가 데이터는 경영진 의사결정의 핵심이며 내부 데이터(HR 인건비, SCM 재료비, Finance 전표)로 자급된다.

### Should (Phase 2 내 포함, HIS 연동 확보 시)

**FR-43 UDI 역추적** — 리콜 대응은 법적 의무에 준하는 환자 안전 요건이다. 단, "환자별 자재 사용기록"은 HIS EMR의 시술기록과 ERP lot을 연결해야 하므로 M2a(HIS EMR 연동 포인트 확정) 게이트가 필요하다. 모델 설계와 역추적 쿼리는 지금 작성 가능하다.

### Could (Phase 2 후반 또는 Phase 3 이관 가능)

**FR-44 바코드/RFID 스캔 빌링** — 하드웨어(스캐너·RFID 리더) 조달, 네트워크 구성, HIS 빌링 API 협의까지 외부 변수가 3개 이상이다. API 계약(페이로드 스펙)과 재고 출고 로직은 선행 구현하고, 하드웨어·HIS 빌링 연동은 외부 준비 완료 후 연결한다.

---

## 3. FR-41: 사전심사 룰엔진 (청구 전 삭감 예방)

### 3.1 목적과 범위

**목적**: 심평원 제출 전 청구건을 조건 기반 룰로 검증하여 삭감 원인을 사전에 차단한다.

**현재 상태**: Claims 모듈은 심사결과통보서 수신·삭감 분류·이의신청(후처리)만 담당. 청구 전 검증 없음.

**In Scope**
- 룰 정의 모델 (조건 집합 → 경고/차단 결정)
- 평가 엔진 — 청구건 컨텍스트를 룰에 대입하여 위반 여부 판정
- 샘플 룰 구현 (중복청구, 상병-수가 불일치, 산정횟수 초과 — 게이트 전 선행 가능한 일반 사례)
- 경고/차단 결과를 청구 화면에 반환하는 API

**Out of Scope**
- 전체 심평원 고시 룰셋 코드화 (협의 후 점진적 추가)
- 심평원 EDI 청구 발송 (HIS/청구 프로그램 영역)

### 3.2 도메인 협의 필요 항목

| # | 협의 항목 | 협의 대상자 | 결정 내용 |
|---|-----------|-----------|---------|
| D41-1 | 코드화할 우선 룰 목록 (삭감 빈도 상위 N건) | 의료보험팀장 | 어떤 삭감 사유코드가 자주 발생하는지 → 룰 우선순위 |
| D41-2 | 각 룰의 조건 구조 (나이 범위, 상병코드 조합, 산정횟수 기준) | 의료보험팀장 + Regulatory 마스터 | 각 룰의 구체적 판정 조건값 |
| D41-3 | 경고 vs 차단 정책 — 어떤 위반은 경고만, 어떤 위반은 청구 차단인가 | 의료보험팀장 + 원장 | 룰별 action 결정 (WARN / BLOCK) |
| D41-4 | 트리거 시점 — 처방 입력 시점인가 청구 등록 시점인가 | 원무과장 + 의료보험팀장 | UI 연계 포인트 결정 |
| D41-5 | 심평원 고시 업데이트 주기와 룰셋 갱신 프로세스 | 의료보험팀장 | 운영 시 룰 유지보수 담당자·주기 |

### 3.3 지금 구현 가능한 골격 (협의 전 착수 가능)

**데이터 모델 골격**

```
claims_rule (룰 정의)
  rule_id        PK
  rule_code      VARCHAR  -- 예: "DUP_CLAIM", "DX_FEE_MISMATCH", "COUNT_EXCEEDED"
  rule_name      VARCHAR
  description    TEXT
  condition_json JSONB    -- 조건 구조 (협의 후 값 채움)
  action         ENUM(WARN, BLOCK)
  severity       ENUM(INFO, WARNING, ERROR)
  effective_from DATE     -- Regulatory temporal 패턴 동일 적용
  effective_to   DATE     -- NULL = 현재 유효
  is_active      BOOLEAN
  created_at, updated_at

claims_rule_evaluation_log (평가 이력)
  eval_id        PK
  claim_item_ref VARCHAR  -- 청구건 식별자 (Claims 모듈 연계)
  rule_id        FK → claims_rule
  evaluated_at   TIMESTAMP
  result         ENUM(PASS, WARN, BLOCK)
  matched_reason TEXT     -- 어떤 조건에서 걸렸는지 기록
  resolved_at    TIMESTAMP  -- WARN 건에 대해 담당자 확인 처리 시점
  resolved_by    FK → users
```

**평가 엔진 골격**

```python
# services/core/src/claims/pre_review/
#   engine.py    -- RuleEngine: load_active_rules() → evaluate(claim_context)
#   conditions/  -- 조건 평가자 (나이, 상병코드, 횟수, 중복)
#   rules/       -- 샘플 룰 구현체
```

엔진은 `RuleEngine.evaluate(claim_context: ClaimContext) -> List[RuleViolation]` 인터페이스로 고정하고, 룰 구현체는 조건-액션 구조를 따르면 플러그인처럼 추가할 수 있도록 설계.

**샘플 룰 (협의 전 구현 가능한 일반 사례 3개)**

| 룰코드 | 설명 | 판정 조건 |
|--------|------|----------|
| `DUP_CLAIM_SAME_DAY` | 동일 환자·동일 날짜·동일 수가코드 중복 청구 | claim_items WHERE patient_id + fee_code + service_date 중복 |
| `COUNT_EXCEEDED` | 동일 수가코드 월 인정횟수 초과 | Regulatory 마스터의 `max_count_per_month` 참조 (값은 협의 후) |
| `DX_FEE_AGE_MISMATCH` | 수가 적용 나이 제한 위반 | 환자 생년월일 × 수가코드 연령 제한 (Regulatory 마스터 참조) |

**API 계약 (Frontend 연동용)**

```
POST /api/v1/claims/pre-review
Request:  { claim_items: [...], patient_id, service_date }
Response: {
  overall_result: "PASS" | "WARN" | "BLOCK",
  violations: [
    { rule_code, rule_name, action, matched_reason, fee_code }
  ]
}
```

### 3.4 외부/협의 의존 항목

| 항목 | 의존 대상 | 선행 여부 |
|------|----------|---------|
| 룰셋 조건값 (나이 범위·상병코드·횟수) | 심평원 고시 + 의료보험팀 해석 | 샘플 룰 구현 후 점진적 추가 가능 |
| 트리거 연동 포인트 (청구 UI) | Frontend 청구 화면 설계 | API 계약 확정 후 연동 |
| Regulatory 마스터 인정기준 필드 추가 | FR-35 수가 마스터 스키마 | Phase 1 Regulatory 완료 후 확장 |

### 3.5 게이트 (협의 완료 판정 기준)

**G41-A (설계 게이트 — 착수 허용 조건)**: 이미 충족. 엔진 골격·샘플 룰은 즉시 착수 가능.

**G41-B (룰셋 1차 배포 게이트)**:
- [ ] D41-1 완료: 우선 룰 목록 10건 이상 확정
- [ ] D41-2 완료: 각 룰의 조건값 문서화 (조건 JSON 스키마로 변환 가능한 수준)
- [ ] D41-3 완료: 룰별 WARN/BLOCK 정책 결정
- [ ] 샘플 룰 3개 단위 테스트 통과

**G41-C (운영 진입 게이트)**:
- [ ] D41-4 완료: 트리거 시점 결정 + 청구 UI 연동 테스트
- [ ] D41-5 완료: 룰 갱신 프로세스 정의
- [ ] 의료보험팀 UAT — 실제 삭감 이력 건으로 역검증 (과거 삭감 건을 룰엔진에 통과시켜 탐지 여부 확인)

---

## 4. FR-42/50: ABC 원가 정밀화

### 4.1 목적과 범위

**목적**: FR-50 1차(진료과별 인건비·삭감 귀속) 완료된 기반 위에 재료비 배부와 활동(Activity) 차원을 추가하여 진료과·시술·의사 개인별 정밀 원가를 도출한다.

**현재 상태**: FR-50 1차 — 진료과별 인건비 배부 + 확정 삭감 손실 귀속 완료. 재료비 배부, 활동 정의, 원가동인 차원은 미구현.

**In Scope**
- 활동(Activity) 마스터 모델
- 원가동인(Cost Driver) 정의 모델
- 활동별 원가 집계 및 배부 엔진 확장
- 진료과·시술·의사 3차원 원가 분석 뷰

**Out of Scope**
- 활동 정의 값, 원가동인 기준값 (협의 후 입력)
- 외부 벤치마크 비교 (별도 기획 필요)

### 4.2 도메인 협의 필요 항목

| # | 협의 항목 | 협의 대상자 | 결정 내용 |
|---|-----------|-----------|---------|
| D42-1 | 활동(Activity) 목록 정의 — 어떤 단위로 원가를 묶을 것인가 | 원가팀(재무팀) + 경영진 | 예: 외래진료, 수술, 입원병동 간호, 검사, 처치 등 N개 |
| D42-2 | 인건비 원가동인 기준 — 시간 기준인가 건수 기준인가 | 원가팀 + HR팀 | 예: 의사=진료건수, 간호사=재원환자일수, 의료기사=검사건수 |
| D42-3 | 재료비 원가동인 기준 — 사용량 직접 배부인가 배부율인가 | 원가팀 + SCM 담당 | FIFO 재고 차감 데이터 활용 방식 결정 |
| D42-4 | 간접비(공통관리비, 시설비 등) 배부 기준 | 원가팀 + 총무팀 | 예: 면적, 건수, 수익 비율 중 선택 |
| D42-5 | 원가 계산 주기 — 월 마감인가 분기인가 | 경영진 | 배치 실행 스케줄 결정 |
| D42-6 | 최소 집계 단위 — 진료과까지인가 의사 개인까지인가 | 경영진 (인사 민감성 고려) | 화면·보고서 집계 레벨 결정 |

### 4.3 지금 구현 가능한 골격 (협의 전 착수 가능)

**데이터 모델 골격**

```
costing_activity (활동 마스터)
  activity_id    PK
  activity_code  VARCHAR  -- 예: "OUTPATIENT", "SURGERY", "WARD_NURSING"
  activity_name  VARCHAR
  department_id  FK → departments  -- 귀속 진료과/부서
  is_active      BOOLEAN

costing_cost_driver (원가동인 마스터)
  driver_id      PK
  driver_code    VARCHAR  -- 예: "VISIT_COUNT", "BED_DAYS", "LABOR_HOURS"
  driver_name    VARCHAR
  driver_type    ENUM(HEADCOUNT, TIME, COUNT, AREA, REVENUE_RATIO)
  data_source    VARCHAR  -- 연동 소스 (HR/SCM/Patient 중 어디서 값을 가져오나)

costing_activity_driver_map (활동-원가동인 연결)
  map_id         PK
  activity_id    FK → costing_activity
  driver_id      FK → costing_cost_driver
  cost_category  ENUM(LABOR, MATERIAL, OVERHEAD)
  weight         DECIMAL  -- 혼합 기준일 때 가중치 (협의 후 설정)

costing_period_result (기간별 원가 계산 결과)
  result_id      PK
  period_year    INTEGER
  period_month   INTEGER
  department_id  FK → departments
  activity_id    FK → costing_activity
  doctor_id      FK → employees  -- NULL이면 진료과 합계
  labor_cost     DECIMAL
  material_cost  DECIMAL
  overhead_cost  DECIMAL
  denial_loss    DECIMAL  -- FR-42: 확정 삭감 손실 귀속
  total_cost     DECIMAL
  revenue        DECIMAL  -- 해당 기간 진료과 수입 (Patient 연계)
  created_at     TIMESTAMP
```

**배부 엔진 확장 골격**

```python
# services/core/src/costing/
#   allocation_engine.py  -- AllocationEngine: run_period(year, month)
#   allocators/
#     labor.py      -- HR 인건비 × 원가동인 배부
#     material.py   -- SCM FIFO 차감 × 직접 귀속
#     overhead.py   -- 간접비 × 배부 기준
#   reporters/
#     department.py -- 진료과별 집계
#     doctor.py     -- 의사별 집계 (D42-6 결정 후 활성화)
```

**FR-42 연계 (확정 삭감 → 원가 귀속)**

FR-40 이의신청 워크플로우 완료 시 `denial_loss` 업데이트 이벤트를 `costing_period_result`에 반영하는 훅 추가. (이미 FR-50 1차에서 귀속 경로가 있으므로 월별 재집계 트리거 추가로 구현 가능)

### 4.4 외부/협의 의존 항목

| 항목 | 의존 대상 | 선행 여부 |
|------|----------|---------|
| 활동 목록 값 (D42-1) | 원가팀 협의 | 모델 스키마 작성 후 대기 |
| 원가동인 기준값 (D42-2~4) | 원가팀 협의 | 엔진 파라미터로 주입 — 협의 완료 후 설정 |
| HR 근무시간 집계 데이터 | HR 모듈 (Phase 1 완료) | 이미 가용 |
| SCM 재료 출고 데이터 | SCM 모듈 (Phase 1 완료) | 이미 가용 |
| 간접비 배부 대상 계정과목 | Finance 전표 계정과목 체계 | Finance Phase 1 완료 후 매핑 |

### 4.5 게이트

**G42-A (모델 설계 게이트 — 즉시 착수 가능)**
- [ ] 활동-원가동인 모델 DDL 작성
- [ ] FR-50 기존 코드에서 재사용 가능한 부분 파악
- [ ] AllocationEngine 인터페이스 정의

**G42-B (1차 배부 실행 게이트)**
- [ ] D42-1 완료: 활동 목록 최소 3개 이상 확정 입력
- [ ] D42-2 완료: 인건비 원가동인 기준 확정
- [ ] D42-3 완료: 재료비 직접 배부 경로 확정
- [ ] 원가팀이 수기 계산한 1개월치와 엔진 결과 대조 (오차 5% 이내)

**G42-C (전체 차원 운영 게이트)**
- [ ] D42-4~6 완료: 간접비·주기·집계 단위 결정
- [ ] 경영진 보고서 포맷 검토 및 승인
- [ ] 의사별 집계 화면 개인정보 처리 방침 확인 (인사 민감 데이터)

---

## 5. FR-43: UDI 역추적 (리콜 자재 사용환자 추적)

### 5.1 목적과 범위

**목적**: 의료기기 UDI(Unique Device Identification)로 리콜 발생 시 해당 자재가 사용된 환자 리스트를 역추적한다.

**현재 상태**: SCM 모듈에 lot 관리(FR-11)는 있으나 "환자별 자재 사용기록"이 ERP에 없음. HIS EMR 시술기록 ↔ ERP lot 연결 고리가 없음.

**In Scope**
- UDI 마스터 모델 (UDI-DI: 기기 고유식별 / UDI-PI: 생산 식별)
- 자재-시술-환자 사용기록 모델 (ERP측)
- 역추적 쿼리 (lot_id → 사용 환자 리스트)
- UDI 입력 화면 (SCM 입고 시 UDI 등록)

**Out of Scope**
- HIS EMR 시술기록 자체 (HIS 영역)
- UDI 사용기록 피드 연동 구현체 (HIS EMR 연동 방식 확정 후)

### 5.2 도메인 협의 필요 항목

| # | 협의 항목 | 협의 대상자 | 결정 내용 |
|---|-----------|-----------|---------|
| D43-1 | UDI-DI / UDI-PI 구분 기준과 현재 병원 의료기기 UDI 관리 현황 | SCM 담당자 + 의료기기 관리팀 | 현재 UDI를 어떻게 관리하고 있는가 (바코드 형식, 시스템) |
| D43-2 | 자재-시술-환자 연계점 — HIS EMR 어느 테이블/API에서 시술기록을 가져오는가 | IT/HIS 담당자 + EMR 벤더 | HIS 시술기록 연동 포인트 (M2a 게이트) |
| D43-3 | 역추적 대상 의료기기 범위 — 어떤 카테고리의 기기에 UDI를 적용하는가 | 의료기기 관리팀 + 안전팀 | 품목 마스터 카테고리 중 UDI 대상 분류 |
| D43-4 | 리콜 알림 수신 체계 — MFDS(식약처) 리콜 공시를 어떻게 수신하는가 | IT팀 | 공공 API 활용 여부 또는 수동 입력 |

### 5.3 지금 구현 가능한 골격 (협의 전 착수 가능)

**데이터 모델 골격**

```
scm_udi_master (UDI 기기 마스터 — UDI-DI)
  udi_id           PK
  udi_di           VARCHAR UNIQUE  -- Device Identifier (GS1/HIBCC/ICCBBA)
  device_name      VARCHAR
  manufacturer     VARCHAR
  model_number     VARCHAR
  device_class     VARCHAR  -- 식약처 등급 (1~4)
  is_implantable   BOOLEAN
  item_id          FK → scm_items  -- SCM 품목 마스터 연결
  created_at       TIMESTAMP

scm_udi_lot (UDI 생산 식별 — UDI-PI, lot과 연결)
  udi_lot_id       PK
  udi_id           FK → scm_udi_master
  lot_id           FK → scm_lots   -- 기존 SCM lot 연결
  udi_pi           VARCHAR  -- Production Identifier (lot번호 + 유효기간 + 일련번호)
  manufacture_date DATE
  expiry_date      DATE
  quantity         INTEGER
  is_recalled      BOOLEAN DEFAULT FALSE
  recall_reason    TEXT
  recalled_at      TIMESTAMP

scm_device_usage_record (자재 사용기록 — HIS 연동 수신처)
  usage_id         PK
  udi_lot_id       FK → scm_udi_lot
  patient_id       FK → patients  -- ERP Patient 모듈 (차트번호 기준)
  his_procedure_id VARCHAR  -- HIS EMR 시술 고유 ID (역추적용 참조키)
  used_at          TIMESTAMP
  department_id    FK → departments
  doctor_id        FK → employees
  quantity_used    INTEGER
  received_from    ENUM(HIS_FEED, MANUAL)  -- 연동 방식 기록
  created_at       TIMESTAMP
```

**역추적 쿼리 골격**

```python
# services/core/src/scm/udi/
#   traceability.py
#
# def trace_recalled_patients(udi_di: str, lot_numbers: List[str]) -> TraceResult:
#   """
#   1. scm_udi_lot에서 is_recalled=True 또는 lot_numbers 해당 lot 조회
#   2. scm_device_usage_record에서 해당 udi_lot_id 사용 환자 조회
#   3. 환자별 사용 일자, 시술 정보, 담당 의사 반환
#   4. 리콜 알림 이력 생성
#   """
```

**API 계약**

```
POST /api/v1/scm/udi/recall-trace
Request:  { udi_di, lot_numbers?: [], recall_reason }
Response: {
  affected_patients: [
    { patient_id, chart_no, used_at, department, doctor, quantity_used }
  ],
  total_count: N,
  trace_id: "...",  -- 역추적 이력 ID
  traced_at: "..."
}
```

### 5.4 외부/협의 의존 항목

| 항목 | 의존 대상 | 선행 여부 |
|------|----------|---------|
| HIS 시술기록 연동 포인트 (D43-2) | M2a 게이트 (HIS EMR 연동 설계) | scm_device_usage_record 피드 구현 전 필요 |
| UDI 스캔 입력 (바코드) | FR-44 바코드 인프라 또는 수동 입력 대체 | 수동 입력으로 먼저 운영 가능 |
| MFDS 리콜 공시 연동 (D43-4) | 공공 API 또는 수동 입력 | 수동 입력으로 먼저 구현, API는 확보 후 |

### 5.5 게이트

**G43-A (모델·쿼리 골격 게이트 — 즉시 착수 가능)**
- [ ] UDI 마스터 모델 DDL 작성
- [ ] scm_lots와 연결 스키마 설계
- [ ] 역추적 쿼리 로직 단위 테스트 (합성 데이터 사용)

**G43-B (HIS 연동 게이트 — M2a 선행 필요)**
- [ ] D43-2 완료: HIS EMR 시술기록 연동 포인트 확정 (API/파일/DB 방식)
- [ ] scm_device_usage_record HIS 피드 어댑터 구현
- [ ] 실 HIS 연동 테스트 (합성 데이터 — 실 환자 데이터 금지)

**G43-C (운영 진입 게이트)**
- [ ] D43-1 완료: 대상 의료기기 카테고리 품목 마스터에 UDI 플래그 추가
- [ ] D43-3 완료: UDI 범위 결정
- [ ] 리콜 시뮬레이션 시나리오 테스트 통과 (합성 리콜 공시 → 역추적 리스트 생성 확인)

---

## 6. FR-44: 바코드/RFID 스캔 → 재고 차감 + 빌링

### 6.1 목적과 범위

**목적**: 수술실·처치실에서 자재 사용 시 바코드/RFID 스캔으로 ERP 재고를 즉시 차감하고 HIS 빌링 데이터를 동시 전송한다.

**현재 상태**: 재고 차감은 수동 입력. 빌링(처방 청구) 데이터 전송은 없음. 바코드/RFID 인프라 미설치.

**In Scope**
- 스캔 수신 API — 바코드/RFID 페이로드 수신 계약 정의
- 재고 차감 이벤트 처리 (FIFO FR-46 확장)
- 빌링 이벤트 발신 페이로드 정의
- 수동 입력 대체 경로 (하드웨어 없을 때 화면 입력)

**Out of Scope**
- 스캐너·RFID 리더 하드웨어 선정·설치 (IT 인프라팀)
- HIS 빌링 API 구현 (HIS 벤더)
- RFID 태그 부착 프로세스 (운영 영역)

### 6.2 도메인/외부 협의 필요 항목

| # | 협의 항목 | 협의 대상자 | 결정 내용 |
|---|-----------|-----------|---------|
| D44-1 | 바코드/RFID 하드웨어 선정 및 통신 방식 (Wi-Fi/유선/BLE) | IT 인프라팀 | 스캐너 모델, 네트워크 구성, ERP API 엔드포인트 접근 경로 |
| D44-2 | HIS 빌링 API 스펙 — 어떤 포맷으로 처방 청구 데이터를 전달하는가 | HIS 벤더 + IT담당 | 빌링 이벤트 페이로드 필드 정의 |
| D44-3 | 스캔 실패 처리 정책 — 미등록 바코드, 재고 부족 시 현장 처리 방법 | 수술실/처치실 담당 간호사 + 원무팀 | 오류 화면 UX, 수동 보정 절차 |
| D44-4 | 빌링 전송 타이밍 — 스캔 즉시인가 수술 종료 후 일괄 전송인가 | 원무팀 + HIS 담당 | 트랜잭션 단위 결정 |

### 6.3 지금 구현 가능한 골격 (협의 전 착수 가능)

**스캔 수신 API 계약 (페이로드 목업)**

```
POST /api/v1/scm/scan-event
Headers: { Authorization: Bearer <device_token> }
Request:
  {
    scan_type: "BARCODE" | "RFID",
    raw_data: "...",  -- 스캐너에서 수신한 원시 데이터
    barcode_type: "GS1_128" | "QR" | "RFID_EPC",
    scanned_at: "ISO8601",
    scanner_id: "...",  -- 하드웨어 식별자
    location_code: "OR_01",  -- 수술실/처치실 코드
    operator_id: "..."  -- 스캔 담당자 (HIS 로그인 ID 또는 ERP 직원 ID)
  }
Response:
  {
    event_id: "...",
    status: "PROCESSED" | "PARTIAL" | "FAILED",
    inventory_deducted: { item_code, lot_id, quantity, remaining_stock },
    billing_event_sent: boolean,
    error_code?: "ITEM_NOT_FOUND" | "STOCK_INSUFFICIENT" | "BILLING_SEND_FAILED"
  }
```

**재고 차감 이벤트 처리 골격**

```python
# services/core/src/scm/scan/
#   handler.py    -- ScanEventHandler: process(scan_event) → InventoryEvent + BillingEvent
#   decoder.py    -- 바코드/RFID raw_data → item_code + lot_id 디코딩 (GS1, RFID EPC)
#   billing_adapter.py  -- BillingAdapter 인터페이스 (HIS API 구현체는 D44-2 후 교체)
```

**빌링 어댑터 인터페이스**

```python
class BillingAdapter(ABC):
    @abstractmethod
    async def send_billing_event(
        self, patient_id: str, item_code: str,
        quantity: int, service_datetime: datetime
    ) -> BillingResult:
        """HIS 빌링 API로 처방 청구 데이터 전송. 구현체는 협의 후 교체."""
        ...
```

**수동 입력 대체 화면**

스캐너 없이 품목 코드·수량·환자를 직접 입력하여 동일 `scan-event` API를 호출하는 간이 화면. 하드웨어 준비 전 운영 가능.

### 6.4 외부/협의 의존 항목

| 항목 | 의존 대상 | 선행 여부 |
|------|----------|---------|
| 하드웨어 연결 구현 (D44-1) | IT 인프라팀 하드웨어 조달·설치 | API 계약은 지금 확정 가능, 하드웨어는 외부 |
| HIS 빌링 API 구현체 (D44-2) | HIS 벤더 협의 | 어댑터 인터페이스는 지금 정의, 구현은 협의 후 |
| 수술실 네트워크 구성 (D44-1) | IT 인프라팀 | 하드웨어와 동반 결정 |
| RFID EPC 디코딩 라이브러리 | 하드웨어 벤더 스펙 | 벤더 결정 후 라이브러리 선정 |

### 6.5 게이트

**G44-A (API 계약 게이트 — 즉시 착수 가능)**
- [ ] POST /scan-event API 계약 문서 확정
- [ ] BillingAdapter 인터페이스 정의
- [ ] Mock BillingAdapter 구현 (테스트용)
- [ ] 수동 입력 대체 화면 구현 (하드웨어 없이 동작)

**G44-B (HIS 빌링 연동 게이트)**
- [ ] D44-2 완료: HIS 빌링 API 스펙 확정
- [ ] BillingAdapter HIS 구현체 개발
- [ ] 통합 테스트 (Mock HIS → 실 HIS 전환)

**G44-C (하드웨어 연동 게이트)**
- [ ] D44-1 완료: 하드웨어 선정 및 설치
- [ ] D44-3 완료: 스캔 실패 처리 정책 결정
- [ ] D44-4 완료: 빌링 타이밍 정책 결정
- [ ] 수술실 현장 테스트 (실 장비 스캔 → ERP 재고 차감 → HIS 빌링 전송 E2E)

---

## 7. Phase 2 전체 로드맵 및 선후관계

### 7.1 의존성 맵

```
[지금 착수 가능 — 협의 없이 선행]
  FR-41 엔진 골격 + 샘플 룰 3개
  FR-42 모델 스키마 + AllocationEngine 인터페이스
  FR-43 UDI 마스터 모델 + 역추적 쿼리 골격
  FR-44 scan-event API 계약 + BillingAdapter 인터페이스 + 수동 입력 화면
         ↓
[M2a: HIS EMR 연동 포인트 확정]
  FR-43 G43-B: scm_device_usage_record 피드 어댑터
  FR-44 G44-B: HIS 빌링 API 구현체
         ↓
[D41 계열 협의: 의료보험팀 룰셋 협의]
  FR-41 G41-B: 룰셋 1차 배포
         ↓
[D42 계열 협의: 원가팀 활동/동인 협의]
  FR-42 G42-B: 1차 배부 실행
         ↓
[D44 계열: 하드웨어 조달·설치]
  FR-44 G44-C: 수술실 현장 테스트
```

### 7.2 협의 없이 선행 가능한 작업 목록

다음 작업은 외부 협의 대기 없이 즉시 착수할 수 있다.

| # | 작업 | 관련 FR | 산출물 |
|---|------|--------|-------|
| P2-S01 | FR-41 claims_rule 모델 DDL + 마이그레이션 | FR-41 | DB 스키마 |
| P2-S02 | FR-41 RuleEngine 인터페이스 + 평가 루프 | FR-41 | engine.py 골격 |
| P2-S03 | FR-41 샘플 룰 3개 구현 (중복·횟수·나이) | FR-41 | 룰 구현체 + 단위 테스트 |
| P2-S04 | FR-41 POST /claims/pre-review API | FR-41 | API 엔드포인트 |
| P2-S05 | FR-42 활동·원가동인 모델 DDL 설계 | FR-42/50 | DB 스키마 확장 |
| P2-S06 | FR-42 AllocationEngine 인터페이스 정의 | FR-42/50 | engine.py 인터페이스 |
| P2-S07 | FR-42 FR-50 기존 코드 리팩토링 (재료비 배부 경로 확보) | FR-42/50 | 코드 확장 |
| P2-S08 | FR-43 scm_udi_master, scm_udi_lot DDL | FR-43 | DB 스키마 |
| P2-S09 | FR-43 역추적 쿼리 함수 + 단위 테스트 (합성 데이터) | FR-43 | traceability.py |
| P2-S10 | FR-43 POST /scm/udi/recall-trace API | FR-43 | API 엔드포인트 |
| P2-S11 | FR-44 scan-event API 계약 문서 | FR-44 | API 스펙 문서 |
| P2-S12 | FR-44 BillingAdapter 인터페이스 + Mock 구현체 | FR-44 | adapter.py |
| P2-S13 | FR-44 ScanEventHandler (재고 차감 연동) | FR-44 | handler.py |
| P2-S14 | FR-44 수동 입력 대체 화면 | FR-44 | UI 컴포넌트 |

### 7.3 단계별 타임라인 (추정)

> 1개발자 + Claude Code 기준. T = Phase 2 착수일.

| 구간 | 기간 | 작업 | 게이트 |
|------|------|------|-------|
| T+0~2주 | 2주 | P2-S01~S14 전체 (선행 가능 골격 일괄 구현) | 각 Gx-A 게이트 |
| T+2~4주 | 2주 | D41 협의 진행 + 룰셋 1차 데이터 입력 | G41-B |
| T+2~4주 | 병행 | D42 협의 진행 + 활동/동인 데이터 입력 | G42-B |
| T+4~6주 | 2주 | M2a(HIS 연동) 확정 후 FR-43 피드 어댑터 | G43-B |
| T+4~6주 | 병행 | D44-2(HIS 빌링) 확정 후 빌링 어댑터 구현 | G44-B |
| T+6~8주 | 2주 | FR-41 룰 확장 (협의 결과 추가), FR-42 전체 배부 실행 | G41-C, G42-C |
| T+8~10주 | 2주 | D44-1(하드웨어) 준비 완료 시 현장 연동 | G44-C |
| T+10~12주 | 2주 | Phase 2 통합 테스트, Zero Script QA, 안정화 | G43-C, 전체 완료 |

**주의**: 하드웨어 조달은 병원 구매 프로세스 기준으로 8~10주 소요 가능. FR-44 G44-C는 하드웨어 납기에 종속되므로 타임라인이 달라질 수 있음. 이 경우 FR-44는 Phase 3으로 이관 가능.

### 7.4 협의 일정 제안 (의존성 순서 기준)

| 협의 회차 | 우선 시점 | 대상 | 목표 |
|-----------|----------|------|------|
| 1차 | T+1주 | 의료보험팀장 | D41-1~3 결정 (룰셋 우선순위 및 WARN/BLOCK 정책) |
| 2차 | T+1~2주 | 원가팀 + 경영진 | D42-1~3 결정 (활동 목록 및 인건비·재료비 원가동인) |
| 3차 | T+2~3주 | HIS IT 담당 | D43-2 + D44-2 (HIS 연동 포인트 및 빌링 API) |
| 4차 | T+3~4주 | IT 인프라팀 | D44-1 (하드웨어 선정 및 네트워크 구성) |
| 5차 | T+4주 이후 | 의료보험팀장 (후속) | D41-4~5 (트리거 시점 및 룰 갱신 프로세스) |

---

## 8. 기능 요구사항 요약

### 8.1 Phase 2 신규 FR (확장)

| ID | Requirement | Priority | Phase | 게이트 조건 |
|----|-------------|----------|:---:|------------|
| FR-41 | 사전심사 룰엔진 — 청구건 고시 기준 검증·경고/차단 (룰 모델 + 평가엔진 + 샘플 룰) | High | 2 | G41-A 즉시 / G41-B D41-1~3 완료 후 |
| FR-42 | 확정 삭감 손실 ABC 원가 귀속 (FR-50 확장 — 재료비 배부 + 활동 차원) | Medium | 2 | G42-A 즉시 / G42-B D42-1~3 완료 후 |
| FR-43 | UDI 역추적 — 리콜 자재 사용 환자 역추적 (모델 + 쿼리 골격 + HIS 피드 연동) | Medium | 2 | G43-A 즉시 / G43-B M2a 완료 후 |
| FR-44 | 바코드/RFID 스캔 → 재고 차감 + 빌링 이벤트 발신 | Medium | 2 | G44-A 즉시 / G44-B D44-2 완료 후 / G44-C 하드웨어 준비 후 |
| FR-50 | ABC 원가 정밀화 — 활동·원가동인 모델 + 배부 엔진 (FR-42와 통합 설계) | Medium | 2 | G42 동일 |

### 8.2 비기능 요건 (Phase 2 추가)

| Category | Criteria | 비고 |
|----------|----------|------|
| 룰엔진 응답 | 청구건 1건 평가 < 200ms (룰 100개 기준) | 인라인 검증이므로 사용자 대기 직결 |
| 원가 배치 실행 | 월 마감 배치 완료 < 30분 (1개월치 전체) | 업무 시간 외 실행 전제 |
| UDI 역추적 | 리콜 쿼리 응답 < 5초 (전체 lot 스캔) | 리콜 긴급 상황 대응 |
| 스캔 이벤트 처리 | scan-event API 응답 < 500ms | 수술실 현장 실시간 피드백 |
| 감사 기록 | 역추적 실행·룰 위반 로그 append-only 보관 | 환자 안전·삭감 증빙 |

---

## 9. 리스크

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| 도메인 협의 지연 (의료보험팀·원가팀 업무 과부하) | High | High | 선행 가능 골격을 먼저 구현하여 협의 대기 중 공수 낭비 최소화. 협의 목표를 게이트 기준으로 명시하여 협의 산출물 범위 한정 |
| 심평원 고시 룰 해석 오류 — AI 생성 룰 무검증 채택 위험 | High | Medium | 모든 룰은 의료보험팀 검토·확인 필수. 룰 등록 시 `confirmed_by` + `confirmed_at` 필드 기록 의무화 |
| HIS EMR 연동 포인트 미확정 (FR-43, FR-44 차단) | High | High | M2a 게이트를 별도 마일스톤으로 관리. 연동 전까지 수동 입력 경로로 운영 |
| 하드웨어 조달 지연 (FR-44) | Medium | High | API 계약·어댑터·수동 입력 화면을 먼저 완성. 하드웨어는 G44-C 게이트로 분리하여 전체 일정 차단 방지 |
| ABC 원가 결과 신뢰성 — 활동·동인 정의가 부정확할 경우 경영 판단 오류 | High | Medium | G42-B에서 원가팀 수기 계산 대조 의무화. 최초 3개월은 참고 지표로만 활용, 실적 검증 후 공식 지표 격상 |
| UDI 사용기록 누락 — HIS 연동 전 공백 기간 | Medium | High | 연동 전 수동 입력 화면으로 대체, 입력 시 `received_from=MANUAL` 기록. 연동 후 소급 입력 절차 정의 |
| 개인정보 처리 (의사별 원가 조회) | Medium | Medium | D42-6에서 집계 레벨 결정 시 인사 민감 데이터 처리 방침 확인. 조회 감사 로그 필수 |

---

## 10. 성공 기준

### 10.1 Phase 2 Definition of Done

- [ ] FR-41: 룰엔진 배포, 룰셋 10건 이상 운영, 의료보험팀 UAT 통과 (과거 삭감 이력 역검증)
- [ ] FR-42/50: 활동·원가동인 배부 엔진 운영, 원가팀 수기 계산 대조 오차 5% 이내
- [ ] FR-43: UDI 마스터 등록 운영 중, 역추적 쿼리 5초 이내, HIS 연동 또는 수동 입력 경로 중 1개 이상 동작
- [ ] FR-44: scan-event API + 수동 입력 화면 동작, HIS 빌링 어댑터 구현 (하드웨어는 G44-C 조건부)
- [ ] 신규 기능 단위 테스트 커버리지 ≥ 70%
- [ ] Gap 분석 Match Rate ≥ 90%

### 10.2 비즈니스 성공 기준 (운영 후 측정)

| 기준 | 목표값 | 측정 방법 |
|------|-------|---------|
| 사전심사 탐지율 | 과거 삭감 이력 중 룰엔진 탐지 가능 비율 ≥ 50% (1차 룰셋 기준) | 역검증: 과거 삭감 건을 룰엔진에 통과시켜 WARN/BLOCK 탐지 확인 |
| ABC 원가 계산 적용 | 월 마감 후 전 진료과 원가 리포트 자동 생성 | 배치 실행 이력 |
| UDI 등록률 | 신규 입고 의료기기 중 UDI 등록 비율 ≥ 80% | scm_udi_lot 등록 건수 / 전체 입고 의료기기 건수 |

---

## 11. 다음 단계

1. [ ] **즉시 착수**: P2-S01~S14 선행 가능 골격 구현 (`/pdca design hospital-erp-phase2` → Design 문서 작성)
2. [ ] **1차 협의 예약**: 의료보험팀장과 D41-1~3 협의 일정 확정 (T+1주 목표)
3. [ ] **2차 협의 예약**: 원가팀과 D42-1~3 협의 일정 확정 (T+1~2주 목표)
4. [ ] **M2a 일정 협의**: HIS IT 담당자와 EMR 연동 포인트 확인 일정 조율 (D43-2, D44-2 동시 진행)
5. [ ] Phase 2 완료 후 Phase 3(안정화·Zero Script QA ≥ 85%) 진입

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-06-08 | Phase 2 초안 — 4개 기능 도메인 협의 선행 설계 기획서. 골격/협의 항목 분리, 게이트 정의, 선행 가능 작업 목록, 우선순위 근거 | weruby |
