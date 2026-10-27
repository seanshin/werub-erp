# 측정방법론서 — 포괄손익계산서 (K-IFRS 1001)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/finance/statements.py::comprehensive_income`

## 1. 산식
```
당기순이익 = 수익 − 비용 (income_statement, 결산분개 제외)
OCI = Σ 기간 OCI계정 순증감(대변−차변): 3400 재평가잉여금, 3410 보험수리적손익
총포괄손익 = 당기순이익 + OCI
```
- 대변 증가=OCI 이익(+), 차변=OCI 손실(−). closing-ifrs 전기(보험수리 재측정) 포함.

## 2. 엔드포인트
- `GET /finance/statements/comprehensive-income?start=&end=`.

## 3. 예시 (테스트 근거)
- 순이익 60만 + 재평가 +30만 − 보험수리손실 10만 → OCI +20만 → 총포괄손익 80만.
- 검증: `tests/test_comprehensive_income.py::test_comprehensive_income`.

## 4. 증빙 (재현)
1. `GET /finance/statements/comprehensive-income?start=YYYY-01-01&end=YYYY-12-31`.
2. 당기순이익(손익계산서)·OCI 항목·총포괄손익 일치 확인.
