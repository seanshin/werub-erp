# 측정방법론서 — 등록형 공시 (1024·1037·1010)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/finance/disclosure_service.py`

## 1. 데이터 모델 (마이그 0096)
- `finance.related_parties`(특수관계자 마스터: name·relation) + `related_party_txns`(period·txn_type·amount·balance)
- `finance.contingencies`(period·kind·description·estimate·likelihood·status)
- `finance.subsequent_events`(period·event_date·description·adjusting·reflected)

## 2. 조회·등록
- `registry(year)` → 연도 특수관계자 거래·우발·후속사건. `notes(year)`에 `registry` 포함.
- add_related_party / add_rp_txn / add_contingency / add_subsequent_event.

## 3. 엔드포인트
- `POST /finance/related-parties`·`/related-parties/transactions`·`/contingencies`·`/subsequent-events`(finance.statements write)
- `GET /finance/disclosures?year=` → auto_notes + registry + notes.

## 4. 예시 (테스트 근거)
- 이사 보수 1.2억·소송 우발부채(추정 5천만)·후속 비수정사건 등록 → registry 반영.
- 검증: `tests/test_disclosure_registry.py::test_related_party_and_registry`.
