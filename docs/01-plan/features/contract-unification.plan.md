# 계약 통합 — 의료계약↔위수탁계약 생애주기 일원화 계획서

## Context
CLM 생애주기 세분화(#503/#504)로 **위수탁계약(scm.OutsourcingContract)**은 8단계 상태머신
(DRAFT→PENDING_APPROVAL→APPROVED→ACTIVE→EXPIRING→EXPIRED→TERMINATED|REJECTED)·승인 직무분리·
발효·금액변경 이력(scm.contract_amendments)·만기 자동전이·e-sign 완료→발효를 갖췄다. 반면
**의료진계약(hr.MedicalStaffContract)**은 여전히 coarse:
- 상태 3개(`ACTIVE|ENDED|DRAFT`)·CHECK 하드코딩(`0018`). 승인 워크플로 없음(create 즉시 ACTIVE).
- 발효(effective) 추적 없음·해지사유/승인자 없음·변경(amendment) 이력 없음.
- e-sign 완료 미러는 sign_* 필드만 갱신, **발효 전이 없음**(위수탁만 자동 activate, `inbound_consumer.py:698`).
- 갱신은 신 계약 생성+이전 자동 ENDED(succession)로 암묵 처리, 명시 renew/amend 부재.

**목표**: 두 계약의 **상태·용어·전이·발효·이력·e-sign 트리거를 단일 커널로 일원화**. 도메인 특수성
(의료=인센티브·전속·employee 승계 / 위수탁=월위탁료 accrue·직접 sign)은 각 서비스에 유지하되, 생애주기
공통부는 `erp.common` 커널로 추출(순수 커널 — scm·hr 모두 의존 허용).

## 아키텍처 결정
- `erp.common`은 **순수 커널**(import-linter: 도메인·app 역류 금지, scm/hr는 common 의존 허용).
  → 상태머신·이력 테이블을 common으로 끌어올려 양 도메인이 공유. scm→common·hr→common 모두 합법.
- 물리 이력 테이블도 **공통**: `scm.contract_amendments`(0138 신설) → `common.contract_amendments`로 승격,
  `contract_kind(MEDICAL|OUTSOURCING)`+`contract_id`로 다형 식별(스키마 교차 FK 회피).

## 단계별 PR

### PR-1 — 공통 커널 + 통합 이력 테이블 (무동작변경 리팩터, 마이그 0139)
- `erp/common/contract_lifecycle.py`(신규, 순수): 상태 상수·`TRANSITIONS` 가드맵·`ACCRUABLE`·
  `EXPIRING_SOON_DAYS`·`STATUS_SQL`(CHECK 절 재사용)·`assert_transition(current,to)`.
- `common.models.ContractAmendment`(신규): contract_kind·contract_id·change_type(AMOUNT|PERIOD|RENEW)·
  old/new_value·effective_on·reason·created_by·created_at. 인덱스 (contract_kind,contract_id).
- 마이그 0139: `common.contract_amendments` 생성 → `scm.contract_amendments` 행 복사
  (kind='OUTSOURCING') → scm 테이블·인덱스 DROP. downgrade 역방향 복원.
- `scm.contract_service`/`scm.models` 리팩터: 로컬 `_TRANSITIONS`/`_ACCRUABLE` 제거·커널 사용,
  `ContractAmendment`는 common에서 import·`contract_kind="OUTSOURCING"` 기록. scm.models의 ContractAmendment 제거.
- 기존 scm 테스트 import 경로 갱신. **위수탁 동작·검증 무변경**.

### PR-2 — 의료계약 생애주기 (마이그 0140)
- `hr.MedicalStaffContract`: status `String(16)`·CHECK를 커널 STATUS_SQL로 확장(`ENDED`→`TERMINATED` 백필),
  신규 컬럼 effective_on(valid_from 백필)·approved_by·approved_at·terminate_reason·terminated_on.
- `hr.contract_service` 확장: create(require_approval opt-in·기본 ACTIVE 호환 유지)·submit_for_approval·
  approve(직무분리 작성자≠승인자)·reject·activate(발효일 검증)·terminate(사유)·renew/amend(common 이력 기록,
  kind='MEDICAL'). 커널 `assert_transition` 사용. 기존 create succession(이전 ACTIVE 자동종료)은 TERMINATED로.
- `hr/router.py`: EP submit·approve·reject·activate·terminate(reason)·renew·amend (`hr.payroll` write, IDOR 유지).
- 테스트: 승인 직무분리·발효 가드·불법전이·해지사유·이력 + 기존 5건 무회귀.

### PR-3 — e-sign 발효 트리거 통일 (+ 만기 배치 통합 검토)
- `inbound_consumer._sign_completed`: 발효 트리거를 **양 엔티티 공통**으로 일반화(현 위수탁 전용 →
  medical+outsourcing 모두 COMPLETED+DRAFT/APPROVED→activate, 발효일 도래 시·비차단).
- 의료계약 만기 자동전이: 위수탁 `run_lifecycle_transitions` 패턴을 의료에도 적용할지 검토(의료는
  월 accrue 없음·EXPIRING/EXPIRED 표시만 필요 → 경량 배치 또는 list 표시보정 우선).
- 테스트: 의료 서명완료→발효(도래/미도래).

## Critical files
- `erp/common/contract_lifecycle.py`(신규 커널) · `erp/common/models.py`(ContractAmendment) · 마이그 0139
- `scm/contract_service.py`·`scm/models.py`(커널 재사용 리팩터)
- `hr/models.py`(status 확장·컬럼) · `hr/contract_service.py`(생애주기) · `hr/router.py`(EP) · 마이그 0140
- `integrations/inbound_consumer.py`(발효 트리거 일반화)
- 재사용: scm 생애주기 패턴(#503/#504)·po_service 직무분리·기존 e-sign 미러 경로

## Verification
- `pytest tests/test_contract_lifecycle.py tests/test_medical_contract.py tests/test_sign_mirror.py
  tests/test_outsourcing_contract.py tests/test_month_end.py -q` + 전체 스위트 무회귀.
- import-linter: common 순수 커널 유지(도메인 역류 0)·scm/hr→common 합법.
- 마이그 0139 이력 이동(복사 후 DROP) 무손실·0140 의료 status 확장 무손실(ENDED→TERMINATED).

## 범위 외(후속)
- N단계 결재선 엔진(현 단일 승인) · 의료/위수탁 외 일반 구매계약(CLM 확장) · 계약 PDF 자동생성 ·
  의료계약 월 비용 accrue(급여 경로와 별도 — 현 미적용 유지).
