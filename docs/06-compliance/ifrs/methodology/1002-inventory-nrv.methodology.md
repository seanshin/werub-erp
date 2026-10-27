# 측정방법론서 — 재고 저가법 NRV (K-IFRS 1002)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/scm/valuation_service.py`

## 1. 산식
```
품목 장부가 = Σ(로트 잔량 × 매입단가)         # 로트 기준(FIFO 소진 반영)
가중평균단가 = 장부가 / 잔량합
NRV 평가액 = 잔량합 × NRV단가(item.nrv_unit)   # nrv_unit 미설정 시 평가 제외
평가손실   = max(0, 장부가 − NRV평가액)
목표충당   = Σ 평가손실,  조정차액 = 목표충당 − 현재충당(1159 대변)
```
- 구현: `ValuationService.nrv_preview` / `nrv_post` / `_allowance_balance`.

## 2. 결산분개 (차액만)
- 증가: `차) 재고자산평가손실 5540 / 대) 재고자산평가충당금 1159`; 환입은 반대.
- 전표: `source_module="closing-ifrs"`, `source_ref="inventory-nrv:{yyyymm}"`, `voucher_on`=월말,
  `allow_closed=True`, 멱등.

## 3. 가정 / 데이터 원천
| 입력 | 원천 | 비고 |
|---|---|---|
| 로트 잔량·매입단가 | `scm.lots`(remaining_qty>0) | 장부가·가중평균 |
| NRV 단가 | `scm.items.nrv_unit` | 수기 입력 |
| 원가흐름 | `scm.items.cost_method` | FIFO/WAVG |
| 현재 충당 | 계정 1159 원장 | 대변잔액 |

## 4. 예시 계산 (테스트 근거)
- A: 잔량100×원가1,000=장부가10만, NRV단가700→NRV7만 → 손실3만. B(원가<NRV)·C(NRV미설정) 제외.
- 검증: `tests/test_inventory_nrv.py::test_nrv_preview`, `::test_nrv_post_idempotent`.

## 5. 증빙 (재현 절차)
1. 품목 NRV 설정 `PATCH /inventory/items/{id}/nrv`.
2. `GET /inventory/nrv/preview?yyyymm=` → 품목별 손실·차액.
3. `POST /inventory/nrv/post?yyyymm=` → 결산분개 전기.
4. 재무상태표에서 1159 차감 반영 확인.
- 마이그: `0089_inventory_nrv.py`. 감사로그: `INVENTORY_NRV_POST`.
- 경계: scm→finance 단방향(전표는 finance VoucherService 경유). finance↛scm 유지(NRV 사전점검은 재고화면).
