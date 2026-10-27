# 측정방법론서 — 리스 정밀화 (K-IFRS 1116)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/finance/lease_service.py`

## 1. 면제(단기·소액)
- create(exempt=True): 부채/사용권 0, 개시전표 없음. accrue: `차) 단기·소액리스료 5645 / 대) 현금 1110`(월 지급액).

## 2. 재측정
```
잔여기간 = 신총기간 − 경과
신부채 = PV(신지급액, 잔여기간, 월이율)
delta = 신부채 − 현재부채 → 사용권자산 동액 조정(1720/2210)
```
- 구현 `LeaseService.remeasure`. 멱등(source_ref=lease-remeasure:{id}:{date}).

## 3. 만기분석
- 잔여 각 월 지급액을 만기버킷(≤1년/1-2년/2-5년/>5년) 미할인 배분 + 현재부채(현가) 합. `LeaseService.maturity`.
- 주석 자동: DisclosureService.auto_notes → note_key=lease_maturity.

## 4. 예시 (테스트 근거)
- 면제: 월 20만 → 5645/현금 20만. 재측정: 잔여11×120만=1,320만(delta +220만). 만기: 24개월×50만 → ≤1년 600만·1-2년 600만.
- 검증: `tests/test_lease_refinement.py`.

## 5. 증빙
- `POST /finance/leases`(exempt)·`/leases/{id}/remeasure`·`GET /finance/disclosures`(lease_maturity). 마이그 0095.
