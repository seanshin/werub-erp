# 측정방법론서 — 수익인식 미청구수익 (K-IFRS 1115)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/finance/revenue_recognition_service.py`

## 1. 미청구수익 발생·환입
- 발생(기간말): `차) 계약자산 1135 / 대) 의료수익 4110` (추정 발생액)
- 환입(차기초): `차) 의료수익 4110 / 대) 계약자산 1135` — 실제 청구 시 정상 수익과 상계
- 멱등(기간): source_ref=unbilled:{yyyymm}·unbilled-rev:{yyyymm}.

## 2. 엔드포인트
- `POST /finance/unbilled-revenue/accrue?yyyymm=&amount=`·`GET /finance/unbilled-revenue/status`(finance.periods).

## 3. 예시 (테스트 근거)
- 2026-05 300만 발생 → 5/31 1135/4110, 6/1 4110/1135 환입.
- 검증: `tests/test_revenue_recognition.py::test_accrue_and_reverse`, `::test_idempotent_and_validation`.

## 4. 한계
- 진행률 자동 산정 미구현(추정액 입력). 계약부채는 선수금(2140)·2145 계정으로 인식(검진권 기존).
