# 위수탁계약(CLM) 생애주기 단계 세분화 — 계획서

> 작성 2026-06-29 · 도메인 SCM(위수탁계약) · 의료계약 통합은 범위 외(후속).

## 1. 배경 (Context)
미커버 도메인 검토에서 **일반 구매/위수탁 계약(CLM)**이 남은 大 갭으로 식별됨. 현재
`scm.OutsourcingContract`는 `status = ACTIVE|EXPIRED|TERMINATED` 3단계로 **coarse**하며 다음이 누락:

| 누락 | 현황 (근거) |
|---|---|
| 내부 승인 단계 | `register` 즉시 ACTIVE — 결재 게이트 없음 (`contract_service.py:17`) |
| 발효(effective) 추적 | valid 기간만 있고 서명완료→발효 시점 상태 없음 |
| EXPIRED 자동전이 | d_day<0는 `list` 표시값일 뿐 DB는 ACTIVE 유지 (`contract_service.py:94`) |
| auto_renew | 모델 필드만 존재, 로직 없음 (`scm/models.py`) |
| 월 위탁료 자동화 | `accrue_month` 멱등 구현됐으나 수동 EP만 — month_end 미통합 (`month_end_service.py:11`) |
| 금액변경·해지사유·갱신이력 | 기간 중 단가 인상=새 계약, 해지 사유/버전 기록 없음 |

**목표**: 위수탁계약을 **세분화된 생애주기 상태머신**으로 다듬고, 자동 전이(만기·갱신)·월위탁료 자동화·
금액변경(amendment)·해지사유·이력을 추가. e-sign(트랙A) 완료가 발효를 트리거. **무중단**(기존 ACTIVE 호환).

## 2. 세분화 상태머신
```
DRAFT ──(submit)──▶ PENDING_APPROVAL ──(approve, 직무분리)──▶ APPROVED
  │                                                            │
  │ (승인 불요 옵션: register 시 APPROVED)                       │ (e-sign 완료 or 수동 activate)
  ▼                                                            ▼
REJECTED                                                     ACTIVE(발효·이행중)
                                                               │
                                  d_day≤60 ─(배치)─▶ EXPIRING ──┤
                                          d_day<0 ─(배치)─▶ auto_renew ? RENEW(기간연장) : EXPIRED
                                              (terminate, 사유) ─▶ TERMINATED
```
- 전이 가드(`_TRANSITIONS` 맵)로 불법 전이 차단. 기존 값 무손실 매핑(ACTIVE/EXPIRED/TERMINATED 유지).

## 3. 변경 사항 (단계별 PR)

### PR-1 모델·마이그(0138)
- `OutsourcingContract.status` CHECK 확장: DRAFT·PENDING_APPROVAL·APPROVED·ACTIVE·EXPIRING·EXPIRED·TERMINATED·REJECTED.
- 신규 컬럼: `approved_by`·`approved_at`·`effective_on`(기본=start_on)·`terminate_reason`·`terminated_on`.
- 신규 테이블 `scm.contract_amendments`(contract_id·change_type[AMOUNT|PERIOD|RENEW]·old/new·effective_on·reason·created_by).

### PR-2 생애주기 서비스 — `scm/contract_service.py`
- `register(require_approval=False)`·`submit_for_approval`·`approve(직무분리 요청자≠승인자, po_service.approve 패턴)`·`reject`.
- `activate`(APPROVED/DRAFT→ACTIVE·발효일 검증)·`amend_amount`(ContractAmendment+단가 갱신)·`renew`(PERIOD 이력)·`terminate(reason)`.
- 전이 가드 `_can_transition`. EP(`scm/router.py`): submit·approve·reject·activate·amend·terminate(reason)·list lifecycle.

### PR-3 자동 전이 배치 — `workers/scheduler.py` `job_contract_lifecycle`(일1회)
- ACTIVE+d_day≤60→EXPIRING. ACTIVE/EXPIRING+d_day<0→ auto_renew면 renew(기간연장·AMENDMENT), 아니면 EXPIRED.
  표시용 EXPIRED를 실 상태전이로 승격.

### PR-4 월 위탁료 자동화 — `app/month_end_service.py`
- ACTIVE/EXPIRING 계약 전체 `accrue_month(yyyymm)` 호출(멱등 `source_ref=contract:{id}:{yyyymm}`). 변경 단가 반영.

### PR-5 e-sign 연동 — `inbound_consumer._sign_completed`·`sign_webhook_router`
- OutsourcingContract COMPLETED 미러 시 `activate()` 트리거(발효일 도래 시 ACTIVE).

## 4. 재사용
`po_service.approve` 직무분리 · `accrue_month` 멱등(`contract_service.py:62`) · `_money_audit` · 상태 CHECK 마이그 패턴(0061 교훈: 제약명 IF EXISTS).

## 5. 검증
- 단위 `tests/test_contract_lifecycle.py`: 전이(정상·불법 차단)·승인 직무분리·자동 만기/갱신·금액변경→accrue·e-sign→activate. 회귀 0(기존 ACTIVE 호환).
- 배치(sqlite 하네스)·월위탁료 멱등·ruff·import-linter(scm→finance·common·audit 경계).

## 6. 범위 외 (후속)
의료계약 통합(통일 상태/용어) · N단계 결재선 엔진 · RFQ/단가계약 연계 · 계약 PDF 자동생성.
