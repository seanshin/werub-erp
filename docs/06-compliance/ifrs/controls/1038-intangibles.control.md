# 통제기술서 — 무형자산·차입원가 (K-IFRS 1038·1023) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 취득·상각·자본화는 finance.assets write 권한자만 | 예방 | 필요 시 | RBAC |
| C2 | 월 상각 멱등(자산·기간) | 예방 | 월 | source_ref |
| C3 | 취득·자본화 감사로그(INTANGIBLE_ACQ·BORROWING_CAPITALIZE) | 적발 | 전기 시 | audit_logs |
| C4 | 차입원가 자본화는 적격자산·기간 판단 후 입력 | 예방 | 필요 시 | 자본화 전표·근거 |
| C5 | 손상/환입 멱등(자산·일자)·환입 손상누계 한도 | 예방 | 필요 시 | source_ref·누계 한도 |
| C6 | 처분 시 차대 균형·재처분 차단·감사로그(INTANGIBLE_IMPAIR/DISPOSE) | 적발 | 전기 시 | 전표균형·status guard·audit |

**권한**: finance.assets read/write. **구현**: `intangible_service`·`finance/router.py`.
