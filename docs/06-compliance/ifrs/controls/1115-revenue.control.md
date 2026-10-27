# 통제기술서 — 수익인식 (K-IFRS 1115) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 미청구수익 발생은 finance.periods write 권한자만 | 예방 | 결산 | RBAC |
| C2 | 발생+차기 환입 자동(발생주의 자기상계) | 예방 | 결산 | 발생·환입 전표 쌍 |
| C3 | 멱등(기간) — 이중 발생 차단 | 예방 | 상시 | source_ref UNIQUE |
| C4 | 발생 시 감사로그(UNBILLED_ACCRUE) | 적발 | 전기 시 | audit_logs |

**권한**: finance.periods read/write. **구현**: `revenue_recognition_service`·`finance/router.py`.
