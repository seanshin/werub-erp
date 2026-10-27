# 통제기술서 — 영업부문 (K-IFRS 1108) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 부문 보고 조회는 analytics.reports read 권한자만 | 예방 | 상시 | RBAC |
| C2 | 부문 수익·손익은 진료과 손익(costing)과 동일 산출(일관) | 예방 | 상시 | costing department_pl |
| C3 | 보고부문 임계(10%) 자동 적용 | 예방 | 상시 | segment_report |

**권한**: analytics.reports read. **구현**: `analytics.segment_report`·`analytics/router.py`.
