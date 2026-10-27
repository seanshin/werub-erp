# 통제기술서 — 포괄손익 표시 (K-IFRS 1001) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 포괄손익 조회는 finance.statements read 권한자만 | 예방 | 상시 | RBAC |
| C2 | OCI는 원천계정(3400·3410) 전기에서 자동 집계(수기 미개입) | 예방 | 상시 | 계정 원장 |
| C3 | 당기순이익은 손익계산서와 동일 산식 재사용(일관) | 예방 | 상시 | income_statement 공유 |

**권한**: finance.statements read. **구현**: `statements.comprehensive_income`·`finance/router.py`.
