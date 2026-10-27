# 측정방법론서 — 이연법인세 (K-IFRS 1012)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/finance/deferred_tax_service.py`

## 1. 산식
```
DTA = (Σ 차감할 일시적차이 잔액) × 유효세율
DTL = (Σ 가산할 일시적차이 잔액) × 유효세율
조정차액 = 목표(DTA·DTL) − 현재(1180·2260) 잔액
```
- 유효세율: `finance.tax_rate_settings`(valid_from ≤ 연말 중 최신), 기본 20% (`deferred_tax_service.rate`).
- 일시적차이 원천(`TEMP_DIFF_SOURCES`): 퇴직급여충당부채(2120)·대손충당금(1190)·손상누계(1815)·
  재고평가충당금(1159) → **DTA(차감할)**; 재평가잉여금(3400) → **DTL(가산할)**. 잔액은 대변기준(cr−d).
- 측정일: 연말 `date(year, 12, 31)`.

## 2. 결산분개 (차액만)
- DTA 증가: `차) 이연법인세자산 1180 / 대) 법인세비용 5810`; 감소는 반대.
- DTL 증가: `차) 법인세비용 5810 / 대) 이연법인세부채 2260`; 감소는 반대.
- 전표: `source_module="closing-ifrs"`, `source_ref="deferred-tax:{year}"`, `voucher_on`=12/31,
  `allow_closed=True`. 멱등(재전기 시 차액 0). (`deferred_tax_service.post`)

## 3. 가정 / 데이터 원천
| 입력 | 원천 | 비고 |
|---|---|---|
| 일시적차이 잔액 | 각 원천계정 원장 누적(연말까지) | 대변잔액 |
| 유효세율 | `finance.tax_rate_settings` | 설정·시점 |
| 현재 DTA/DTL | 계정 1180/2260 원장 | DTA=차변, DTL=대변 |

## 4. 예시 계산 (테스트 근거)
- 퇴직급여충당 100만(DTA) + 재평가잉여금 50만(DTL), 세율 20% → DTA 20만, DTL 10만.
- 검증: `tests/test_deferred_tax.py::test_preview_dta_dtl`, `::test_post_idempotent`.

## 5. 증빙 (재현 절차)
1. `GET /finance/deferred-tax/preview?year=YYYY` → 일시적차이·DTA/DTL·차액.
2. `POST /finance/deferred-tax/post?year=YYYY` → 결산분개 전기.
3. 재무상태표에서 1180/2260, 손익에서 5810 반영 확인.
- 마이그: `0088_deferred_tax.py`. 감사로그: `DEFERRED_TAX_POST`. 사전점검(12월): `DEFERRED_TAX_NOT_POSTED`.
