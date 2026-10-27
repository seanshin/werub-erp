# 통제기술서 — 자산손상 환입 (K-IFRS 1036) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 환입은 finance.assets write 권한자만 | 예방 | 필요 시 | RBAC |
| C2 | 환입 한도(손상누계·원가장부가) 자동 검증 | 예방 | 상시 | reverse_impairment 산식 |
| C3 | 멱등(자산·일자) — 이중 환입 차단 | 예방 | 상시 | source_ref |
| C4 | 환입 시 감사로그(IMPAIR_REVERSE) | 적발 | 전기 시 | audit_logs |

**권한**: finance.assets write. **구현**: `asset_service.reverse_impairment`·`finance/router.py`.
