# 통제기술서 — 회계정책·추정변경·오류 (K-IFRS 1008) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 등록은 finance.statements write 권한자만 | 예방 | 필요 시 | RBAC |
| C2 | 처리방법(소급/전진) 명시 등록 | 예방 | 변경 시 | accounting_changes.treatment |
| C3 | 오류수정 실제 정정은 역분개(원전표 보존) | 예방 | 필요 시 | 역분개 전표·사유 |
| C4 | 변경 공시 자동 포함(registry/notes) | 예방 | 결산 | disclosures |

**권한**: finance.statements read/write. **구현**: `disclosure_service`·`finance/router.py`.
