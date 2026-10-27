# 통제기술서 — 재무제표 주석 (K-IFRS 1001) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 정량주석은 GL 계정 증감 자동 산출(수기 미개입) | 예방 | 상시 | _movement 쿼리 |
| C2 | 정성주석 편집은 finance.statements write 권한자만 | 예방 | 결산 | disclosure_notes·감사 |
| C3 | (period,note_key) upsert — 중복 방지 | 예방 | 상시 | UNIQUE |
| C4 | 자동주석은 재무제표(BS/IS)와 동일 원장 사용(일관) | 예방 | 상시 | 계정 원장 공유 |

**권한**: finance.statements read/write. **구현**: `disclosure_service`·`finance/router.py`.
