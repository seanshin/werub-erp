# ERP v1.167.0 → v1.170.0 릴리스 — 심평원 EDI 청구 파운데이션 (산정 엔진·격리 EDI·HIS e결재 갭)

> **Summary**: HIS 원무·심평원 요청(EDI 청구파일 포맷·수가 산정·비급여·묶음 규칙)의 **파운데이션**을
> 사용자 지침대로 **산정(계산) 엔진 선구축 + EDI는 별도 버전드 매핑 엔진으로 격리(시드)** 형태로 착수했다.
> 이어 신규 산정 엔진을 **적대적으로 재검토**해 HIGH 실버그를 포함 4건을 보완하고, **실 진료내역 배선**과
> **EDI 매핑 엔진의 temporal 버전 승격**까지 4개 릴리스(#899~902)로 완결했다. 마이그 **0208**(사전승인
> 채널·응급우회) 외 스키마 불변. **테스트 1347 → 1363** · dev 서버 4회 배포·prod 실측 LIVE.
>
> **핵심 설계 원칙 — 산정(회계 계산식)과 EDI(포맷 매핑)를 완전 분리하고, 양쪽 모두 시점(as-of) 해소로
> 소급 재계산에 안정. B0 실 포맷 스펙·serviceKey는 외부 병목으로 유지(추측 포맷=반려 방지).**

**연관**: 검토 메모 `memory/erp-edi-claim-foundation.md`, 계산식 레퍼런스
`docs/05-reference/calculation-formulas.md §5.6·5.7`, 스코핑 `docs/01-plan/features/edi-claim-generation.plan.md`,
HIS 회신 `docs/05-reference/erp-reply-his-eapproval-gaps-2026-07-19.md`.

---

## 1. 배경

HIS 원무·심평원 트랙에서 "청구 파운데이션(D3) 스텁을 실 청구 생성으로 완성"을 요청받았으나, **실 심평원
EDI 포맷 스펙(B0)·data.go.kr serviceKey·시험청구 요양기호**가 외부 병목이라 무작정 구현 시 잘못된 포맷으로
반려된다. 사용자 지침은 명확했다:

1. **산정 엔진 선구축** — EDI 포맷과 독립인 수가 산정(계산) 코어를 먼저 완성.
2. **EDI는 격리 시드** — 실 서비스 전 별도 작업이므로, 시드 형태로 분리해 제출 경로에 연결하지 않는다.
3. **EDI는 별도 버전드 매핑 엔진** — 포맷 교체가 잦고 시간이 역행해 재계산도 빈번하므로.
4. **회계상 명시 계산식** — 산정 결과의 회계 근거를 코드+문서로 노출.

## 2. 릴리스별 요약

| 릴리스 | PR | 핵심 | 마이그 |
|---|---|---|---|
| **v1.167.0** | #899 | 청구 산정 엔진(`claim_calc.py`) + 격리 EDI 매핑 시드(`edi_mapping.py`) 선구축 | 불변 |
| **v1.168.0** | #900 | HIS e결재 회계연계 재점검 갭 3건 — 발급수수료 유일경로·응급우회·발급취소 | **0208** |
| **v1.169.0** | #901 | 산정 엔진 적대 재검토(HIGH 종별 본인부담률 실버그 등 4건) + 실 진료내역 배선 | 불변 |
| **v1.170.0** | #902 | EDI 매핑 엔진 temporal 버전 승격(발효일 레지스트리·시간 역행 재계산) | 불변 |

## 3. v1.167 — 산정 엔진 선구축 + 격리 EDI 매핑 시드 (D3-B1·B2)

### 3.1 산정 엔진 `claims/claim_calc.py` (회계 계산식)
진료일(as-of) 시점 단가로 명세를 산정하는 **순수 계산 계층**. 기존 자산을 재사용해 중복 로직을 만들지 않았다.

계산식(회계 근거·`calculation-formulas.md §5.6`):
```
라인 금액 = 단가(as-of) × 산정횟수 × (1 + 종별가산율[급여 적용대상])
본인부담  = 급여 금액 × 본인부담률(종별·coverage·산정특례)   [10원 절사 §19]
공단부담(청구액) = 급여 금액 − 본인부담
비급여    = 청구 제외(환자 전액)
묶음      = 패키지 수가(parent) 존재 시 포함 세부/재료(child) 청구 제외
```
- 단가 = `FeeLookupService.get_price`(temporal)·본인부담 분리 = `billing_engine.compute_invoice` 재사용.
- 종별가산 = `InstitutionSurchargeRate`·묶음 = `BundlingRule`. 미리보기 `POST /claims/calc/preview`.

### 3.2 격리 EDI 매핑 시드 `claims/edi_mapping.py`
⚠ **실 심평원 포맷 아님**·`_production_ready=False`·**제출 경로 미연결**. 산정 결과 → EDI 유사 세그먼트
구조로 매핑. 버전 레지스트리(`SEED_v0`)로 포맷 교체 시 새 매퍼 등록·과거 버전 불변. **산정과 완전 분리** —
포맷이 바뀌어도 산정 로직 무변경.

## 4. v1.168 — HIS e결재 회계연계 재점검 갭 3건 (마이그 0208)

HIS 측 문서 재점검에서 **발급 수수료가 ERP로 가는 자동 경로가 아예 없다**는 핵심 누락을 발견해 보완. 3건을
ERP 수신 핸들러로 구현(회신 `erp-reply-his-eapproval-gaps-2026-07-19.md`):

| # | 이벤트 | ERP 처리 |
|---|---|---|
| 1 | `eapproval.document_issued` (필수 유일경로) | 발급은 수수료 확정만·billing 미발행 → 4120 제증명수입 유일 연계(`certificate.issued`와 동일 핸들러·`fee`/`documentId` 수용·멱등) |
| 2 | `preauth.emergency_bypass` (신설) | 응급 우회 발행(마약류·수혈·고가검사) 인지 — `channel=EMERGENCY_BYPASS`·`post_approved=False`(사후승인 미결)·GL 미전기 |
| 3 | `eapproval.document_adjusted` | 발급 취소/환불 → 4120 역분개(`adjustmentId` 멱등) |

+ preauth `channel` PORTAL 명시 · `GET /costing/preauth-summary`에 사후승인 미결 집계 추가. 마이그 **0208**
(`finance.preauth_orders` 컬럼 `channel`·`order_type`·`post_approved`·server_default 안전).

## 5. v1.169 — 산정 엔진 적대 재검토 + 실 진료내역 배선

신규 산정 엔진(회계 금액 산출)을 적대적으로 재검토해 4건을 수정했다.

| 등급 | 결함 | 수정 |
|---|---|---|
| **HIGH(실버그)** | 종별 본인부담률 미배선 — `compute_invoice`에 grade만 넘기고 rates 미주입 → **전 종별에 병원 40% 오적용**(의원30·종합50·상급60 전부 틀림) | `load_billing_rates(treated_on)` 주입(InstitutionGradeSetting 시점관리·surcharge와 동일 어휘) |
| MED | 비급여인데 code_type=TARIFF인 라인에 종별가산 오적용 | 비급여 판정 선행·급여 라인만 가산 |
| MED | 종별가산 `applies_to`(콤마집합) 무시 + [0,1] 클램프 부재 | worklist `resolve_fee_with_mapping` 규약과 정합(>100% 폭주 차단) |
| LOW | 묶음 제외 라인의 거짓 "단가 미해소" 경고 | `priced=True`·0원 처리 |

**실 데이터 배선**: 청구 라인 상세가 `ClaimStatement`(총액만)가 아닌 **`patient.BillingLine`**(HIS
billing.line_recorded 미러)에 영속함을 확인하고, `calc_for_chart(chart_no·care_month·encounter_id)` —
취소 제외·**라인별 진료일 as-of**(다중 진료일 명세) — 을 추가. EP `GET /claims/calc/from-chart`.

## 6. v1.170 — EDI 매핑 엔진 temporal 버전 승격

사용자 재확인("EDI가 실서비스 관련 엔진 형태인지")에 따라 재검증한 결과, EDI를 별도 엔진으로 요구한 **핵심
이유가 "시간 역행 재계산 빈번"**인데 기존 `_MAPPERS`는 단순 이름→함수 dict라 **날짜로 버전을 해소하는
기능이 없었다**(과거 청구를 재생성할 때 그 시점 유효 포맷을 고르지 못함).

- **발효일 기반 temporal 레지스트리** `EdiMappingVersion{version·effective_from·production_ready·mapper}` —
  포맷 교체=새 버전 append·과거 버전 불변(FeeMaster `valid_from/valid_to`와 동일 시점 개념).
- **`resolve_version(as_of)`** — 청구/진료 시점 유효 포맷 선택. 2025 청구를 오늘 재생성해도 2025 당시
  포맷으로 매핑. `map_to_edi(calc, as_of=, version=)` — version 고정 또는 as_of 해소. 각 버전
  `production_ready=False` 유지(격리).

이로써 산정(라인별 as-of 단가)과 EDI(as-of 포맷 버전)가 **양쪽 모두 시점 해소**돼 소급 재계산에 일관 대응.

## 7. 외부 병목 (미해소·추측 금지)

| 병목 | 영향 | 필요 |
|---|---|---|
| **B0 실 EDI 포맷 스펙** | B2'(실 직렬화)·B3(배치)·B4(제출) 착수 불가 | 심평원 공고 별표/개발가이드 |
| **data.go.kr serviceKey** | dev seed → 실 수가 동기화(`job_fee_sync`) | `FEE_API_SERVICE_KEY` 발급·주입 |
| **시험청구 요양기호·시험베드** | 실제 제출·검증(B4) | 심평원 시험베드 접근 |
| **HIS 회신** | v1.168 갭 이벤트 발신 확정 | `document_issued` 필수 발신 여부(최우선) |

## 8. 검증

- **테스트 1347 → 1363**(claim_calc 12건 신규·inbound 4건·costing 1건). ruff 클린·import-linter 7 kept.
- CI(lint·test·migrate 왕복·web build) 전 PR 통과. dev 서버 4회 배포·prod alembic head **0208**·엔드포인트
  인증가드(401) 라이브·health 200 실측.
