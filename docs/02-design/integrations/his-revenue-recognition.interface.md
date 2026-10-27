# HIS 연동 인터페이스 설계 — 수익 발생주의(진행기준·변동대가) [K-IFRS 1115]

> **상태**: HIS 응답 수령·ERP 커넥터 구현(B-1 #396, B-2 #395) — HIS unbilled-progress 엔드포인트 가동 시 동작 · **작성일**: 2026-06-22 · **유형**: 인터페이스 요청서(ERP→HIS)
> **연관**: [ifrs-conformance.plan.md](../../01-plan/features/ifrs-conformance.plan.md) P3-1, billing-sot-decision(D-6),
> [his-billing-lines](his-billing-lines.request.md), 구현 `finance/revenue_recognition_service.py`(미청구수익 발생, 추정액 입력)

## 1. 배경 / 목적

P3-1(수익인식)은 ERP 단독으로 **미청구수익(계약자산 1135) 기간말 발생 + 차기 환입**을 추정액 수기 입력으로 구현했다.
완전한 K-IFRS 1115(발생주의·진행기준)를 위해 **HIS의 진료 진행 데이터**가 필요한 두 항목을 본 문서에서 설계한다:

- **B-1. 진행기준 수익(미청구수익 자동 산정)** — 입원 등 기간 진행 중 수행의무 이행분을 자동 인식.
- **B-2. 변동대가(보험삭감 추정)** — 청구 결정 전, 예상 삭감을 차감한 순액으로 수익 인식.

> SoT 전제(D-6): 진료비 **계산은 ERP**, 진료행위 발생 사실(원천)은 **HIS**. 따라서 진행률·미청구 판정의
> 원천 데이터(진행 중 차트·미청구 행위)는 HIS가 ERP로 제공해야 한다.

## 2. B-1. 진행기준 수익 — 미청구수익 자동 산정

### 2.1 필요 데이터 (HIS → ERP)
기간말 시점, **아직 청구(수납·보험청구)되지 않았으나 진료가 이행된** 입원/외래 건의 진행분:

| 필드 | 설명 | 예 |
|---|---|---|
| `chart_no` | 차트번호(원천 식별, ERP는 집계만) | (PII — 집계 후 폐기) |
| `dept_code` | 진료과 | IM |
| `admission_on` / `as_of` | 입원일 / 기준일 | 2026-06-20 / 2026-06-30 |
| `progress_basis` | 진행률 산정 근거 | 재원일수 / 처치완료 / 정액수가 단계 |
| `incurred_amount` | 기준일까지 이행분(ERP 책정 단가 기반 추정 가능) | 1,200,000 |
| `billed_amount` | 이미 청구·수납된 금액 | 0 |
| `status` | IN_PROGRESS / DISCHARGED_UNBILLED | IN_PROGRESS |

미청구수익 = Σ(`incurred_amount` − `billed_amount`), status가 미청구인 건.

### 2.2 연동 방식 (택1, 협의)
- **(A) 조회 API**: ERP가 결산 시 `GET (HIS)/inpatient/unbilled-progress?as_of=YYYY-MM-DD` 호출 → 위 집계 수신.
- **(B) 이벤트 + 스냅샷**: HIS가 일/월말 진행 스냅샷을 ERP 인박스로 push(`inpatient.progress.snapshot`).
- 권장: **(A) 조회 API** — 결산 시점 on-demand, 정합 단순(기존 his-billing-lines와 동일 패턴).

### 2.3 ERP 처리
- 수신 집계로 `RevenueRecognitionService.accrue_unbilled(yyyymm, amount=Σ)` 자동 호출(추정액 입력 → 자동화).
- 차기 기초 환입(기존 로직) + 실제 청구 시 정상 수익과 상계. dept_code별 분개 확장 가능.

## 3. B-2. 변동대가(보험삭감 추정)

### 3.1 개념
보험청구 결정(승인) 전, 과거 삭감 이력 기반 **예상 삭감률**을 적용해 수익을 순액(예상 결정액)으로 인식.
삭감 추정은 **ERP 보유 데이터로 자체 산출 가능**(claims 삭감 이력·사전심사 적발) — HIS 의존 낮음.

### 3.2 데이터 원천 (대부분 ERP 내부)
| 입력 | 원천 | 비고 |
|---|---|---|
| 청구 예정액(미결정) | ERP claims(결정 전 statement) | 기존 |
| 진료과·항목별 과거 삭감률 | ERP claims 삭감 이력 | 자체 산출 |
| 사전심사 적발 위험 | ERP pre-review risk_score | 기존 |

### 3.3 ERP 처리 (HIS 의존 없음 — 후속 ERP 단독 가능)
- 변동대가 추정 = 청구예정액 × (1 − 예상삭감률). 차이(예상삭감)는 **환불부채/충당** 또는 수익 차감으로 인식.
- 계정: 미청구·미수에서 예상삭감분을 차감(예: 매출차감 or 충당). 신규 계정 검토 시 충돌 점검 필수(과거 5660/4630 사례).
- **본 항목은 HIS 협의 없이 ERP 백로그로 진행 가능** — B-1과 분리.

## 4. PII·보안
- `chart_no` 등 환자식별정보는 **ERP가 집계 목적만** 사용·즉시 폐기(저장 금지) — 기존 사전심사 PII 게이트 원칙 준수.
- 연동키(X-Integration-Key)·HMAC 등 기존 HIS 연동 보안 규약 적용.

## 5. 요청 사항 (HIS 팀)
1. **B-1 조회 API 스펙 확정** — `unbilled-progress` 엔드포인트(필드·진행률 근거·페이지네이션).
2. 진행률 산정 기준 합의(재원일수 vs 처치완료 vs 정액수가 단계) — 진료유형별.
3. 연동 방식(A 조회 / B 이벤트) 선택.

> **B-2(변동대가)는 ERP 단독 진행 가능** — HIS 회신과 무관하게 백로그 착수 가능.
