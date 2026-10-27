# 측정방법론서 — 대손충당금 ECL (K-IFRS 1109)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/finance/allowance_service.py`

## 1. 산식
```
목표충당액 = Σ_bucket ( 매출채권_연령버킷_잔액 × 버킷_손실률 )
조정차액  = 목표충당액 − 현재_대손충당금잔액(1190 대변)
```
- 연령버킷: `settlement_service.aging(as_of, kind="AR")` — 연체일 = as_of − due_on. (current ≤0, 1-30, 31-60,
  61-90, 90+)  (`settlement_service.py:102-119`)
- 버킷 손실률: `finance.allowance_rates`(kind=AR). 미설정 시 기본값 `_DEFAULT_RATES`
  (`allowance_service.py:21`).
- 현재 충당잔액: 계정 1190의 대변−차변 누적(as_of 이하). (`allowance_service._allowance_balance`)

## 2. 결산분개 (차액만)
- delta > 0 (전입): `차) 대손상각비 5690 / 대) 대손충당금 1190` (delta)
- delta < 0 (환입): `차) 대손충당금 1190 / 대) 대손상각비 5690` (|delta|)
- 전표: `source_module="closing-ifrs"`, `source_ref="allowance:AR:{yyyymm}"`, `voucher_on`=월말,
  `allow_closed=True`. 멱등(동일 월 재전기 시 차액 0 → no-op). (`allowance_service.post`)

## 3. 가정 / 데이터 원천
| 입력 | 원천 | 비고 |
|---|---|---|
| 매출채권 잔액·연령 | `finance.receivables`(status≠COLLECTED, amount−collected) | aging |
| 버킷 손실률 | `finance.allowance_rates`(설정·시점) | 관리자 조정 |
| 현재 충당 | 계정 1190 원장 누적 | 대변잔액 |

## 4. 예시 계산 (테스트 근거)
- 미수 100만(31-60, 10%)=10만 + 50만(90+, 50%)=25만 → 목표충당 35만, 현재 0 → 전입 35만.
- 검증: `tests/test_allowance.py::test_preview_computes_ecl`, `::test_post_idempotent_and_reversal`.

## 5. 증빙 (재현 절차)
1. `GET /finance/allowance/preview?yyyymm=YYYY-MM` → 버킷·목표·현재·차액 확인.
2. `POST /finance/allowance/post?yyyymm=YYYY-MM` → 결산분개 전기(voucher_id).
3. 재무상태표(`GET /finance/balance-sheet`)에서 1190 차감 반영 확인.
4. 정정 시 역분개(`POST /finance/vouchers/{id}/reverse`).
- 마이그레이션: `alembic/versions/0087_allowance_doubtful.py`. 감사로그: `ALLOWANCE_POST`.
