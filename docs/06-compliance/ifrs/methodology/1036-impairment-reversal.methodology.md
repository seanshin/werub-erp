# 측정방법론서 — 자산손상 환입 (K-IFRS 1036)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/finance/asset_service.py::reverse_impairment`

## 1. 산식
```
장부가 = 취득원가 + 재평가누계 − 감가상각누계 − 손상누계
환입 = min(회수가능액 − 장부가, 손상누계)     # 손상없을 때 장부가(원가−감가) 한도와 동치
```
- 환입 ≤ 0이면 거부. accumulated_impairment 차감.

## 2. 결산분개
- `차) 손상차손누계 1815 / 대) 유형자산손상차손환입 4640(수익)`. 멱등(자산·일자, source_ref=asset-impair-reverse-…).

## 3. 예시 (테스트 근거)
- book 8M→손상 3M(book 5M). 회수 7M → 환입 min(2M,3M)=2M(book 7M). 회수 20M → 환입 3M 한도(book 8M).
- 검증: `tests/test_asset_impairment.py::test_reverse_impairment`, `::test_reverse_capped_at_accum_impairment`.

## 4. 증빙 (재현)
- `POST /finance/assets/{id}/reverse-impairment?recoverable_amount=&on_date=` → 환입·장부가 확인. 감사로그 IMPAIR_REVERSE.
- 마이그: `0092_impairment_reversal.py`.
