# 측정방법론서 — 공정가치 서열·중간보고·전기비교 (1113·1034·1001)

**상태**: Draft · **작성일**: 2026-06-22

## 1. 공정가치 서열(1113)
- FixedAsset.fair_value_level(재평가 시 설정, 기본 3). `DisclosureService.fair_value_hierarchy` — 수준별 장부가·건수 집계.
- 구현: `asset_service.revalue(fair_value_level=)`, `disclosure_service.fair_value_hierarchy`. 마이그 0097.

## 2. 중간보고(1034)
- `StatementService.interim_report(year, quarter)` — monthly_pl에서 분기/누적/전년 동분기 손익 집계.
- 엔드포인트 `GET /finance/statements/interim?year=&quarter=`.

## 3. 전기비교(1001)
- `StatementService.comparative_balance_sheet(as_of)` — 당기말·전기말(동일 월일 전년) BS + 증감.
- 엔드포인트 `GET /finance/statements/comparative-balance-sheet?as_of=`.

## 4. 예시 (테스트 근거)
- 중간: 1분기 수익 100만·2분기 200만·YTD 300만. 비교BS: 당기 700만·전기 200만·증감 500만. FV: Level2 1억·Level3 3천만.
- 검증: `tests/test_p2b_presentation.py`.
