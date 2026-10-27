# 측정방법론서 — 회계정책·추정변경·오류 (K-IFRS 1008)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/finance/disclosure_service.py`

## 1. 데이터 모델 (마이그 0099)
- `finance.accounting_changes`(period·change_type[POLICY/ESTIMATE/ERROR]·standard·description·treatment[RETROSPECTIVE/PROSPECTIVE]·effect_amount).

## 2. 등록·조회
- `add_accounting_change(...)`, `registry(year)`·`notes(year)`에 accounting_changes 포함.
- 엔드포인트 `POST /finance/accounting-changes`(finance.statements write), `GET /finance/disclosures`.

## 3. 예시 (테스트 근거)
- 정책변경(FIFO→가중평균, 소급, 1,200만)·추정변경(내용연수, 전진)·오류수정(소급, 300만) 등록 → registry.
- 검증: `tests/test_accounting_changes.py::test_accounting_change_registry`.

## 4. 한계
- 소급 재작성 실제 전기는 전표/역분개로 수행. 자동 비교표시 재생성은 후속.
