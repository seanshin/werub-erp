# 통제기술서 — 외화환산 (K-IFRS 1021) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 환산 전기는 finance.periods write 권한자만 | 예방 | 결산 | RBAC |
| C2 | 원화 계좌 환산 차단 | 예방 | 상시 | preview 검증(KRW 거부) |
| C3 | 멱등(계좌·기간) | 예방 | 상시 | source_ref UNIQUE |
| C4 | 환산 시 감사로그(FX_TRANSLATE) | 적발 | 전기 시 | audit_logs |

**권한**: finance.periods read/write. **구현**: `forex_service`·`finance/router.py`.
