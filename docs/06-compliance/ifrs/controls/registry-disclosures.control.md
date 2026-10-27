# 통제기술서 — 등록형 공시 (1024·1037·1010) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 등록은 finance.statements write 권한자만 | 예방 | 필요 시 | RBAC |
| C2 | 특수관계자 거래·우발·후속사건 연도별 등록·주석 자동 포함 | 예방 | 결산 | registry/notes |
| C3 | 후속 수정사건 결산 반영 여부(reflected) 추적 | 적발 | 결산 | subsequent_events |

**권한**: finance.statements read/write. **구현**: `disclosure_service`·`finance/router.py`.
