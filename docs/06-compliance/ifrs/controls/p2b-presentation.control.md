# 통제기술서 — 공정가치·중간보고·전기비교 (1113·1034·1001) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 조회는 finance.statements read 권한자만 | 예방 | 상시 | RBAC |
| C2 | 공정가치 서열은 재평가 시 입력(fair_value_level), 자동 집계 | 예방 | 재평가 시 | revalue·fair_value_hierarchy |
| C3 | 중간보고·비교BS는 동일 원장 산출(일관) | 예방 | 상시 | monthly_pl·balance_sheet 공유 |

**권한**: finance.statements read·assets write(재평가). **구현**: statements·disclosure·asset_service.
