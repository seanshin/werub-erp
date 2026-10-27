# 측정방법론서 — 영업부문 (K-IFRS 1108)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/analytics/service.py::segment_report`

## 1. 산식
```
부문 수익·손익 = costing.CostSnapshotService.department_pl(yyyymm)  # 진료과별
부문 자산 = Σ(취득원가+재평가누계−감가누계−손상누계) by department_id, status≠DISPOSED  # FixedAsset
보고부문 = 수익≥10%총수익 OR |손익|≥10%|총손익| OR 자산≥10%총자산
```
- 구현 `AnalyticsService.segment_report`. 경계: analytics→costing/finance(허용).

## 2. 엔드포인트
- `GET /analytics/segments?yyyymm=` (analytics.reports).

## 3. 예시 (테스트 근거)
- 내과 수익 1천만·자산 4천만 → 보고부문. 정형외과 수익 100만(<10%) → 기타.
- 검증: `tests/test_segments.py::test_segment_report`.

## 4. 증빙
- `GET /analytics/segments?yyyymm=YYYY-MM` → 부문별 수익·손익·자산·reportable. finance-statements 부문 카드.
