# 통제기술서 — 리스 정밀화 (K-IFRS 1116) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 리스개시·재측정은 finance.assets write 권한자만 | 예방 | 필요 시 | RBAC |
| C2 | 월 회계·재측정 전표 멱등 | 예방 | 상시 | source_ref UNIQUE |
| C3 | 재측정 시 감사로그(LEASE_REMEASURE) | 적발 | 전기 시 | audit_logs |
| C4 | 면제 판정(단기·소액)은 개시 시 명시(exempt) | 예방 | 개시 | lease_contracts.exempt |
| C5 | 만기분석 주석 자동 산출 | 예방 | 결산 | disclosure lease_maturity |

**권한**: finance.assets write. **구현**: `lease_service`·`finance/router.py`.
