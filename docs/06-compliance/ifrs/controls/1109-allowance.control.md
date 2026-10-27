# 통제기술서 — 대손충당금 (K-IFRS 1109) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

## 1. 통제활동
| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 충당률 설정 변경은 finance.periods write 권한자만 | 예방 | 변경 시 | 설정 테이블·감사로그 |
| C2 | 충당 전기는 결산 미리보기(preview) 검토 후 전기(post) | 예방 | 월결산 | preview 응답·전표 |
| C3 | 전기 전표 멱등(월별 1전표) — 중복 전기 차단 | 예방 | 상시 | source_ref UNIQUE |
| C4 | 전기 시 감사로그 기록(ALLOWANCE_POST, 차액·기간) | 적발 | 전기 시 | audit_logs(해시체인) |
| C5 | 마감 사전점검에 '대손충당 미전기' 경고 | 적발 | 월결산 | preclose_check.warnings |
| C6 | 정정은 직접수정 금지·역분개만 | 예방 | 필요 시 | 역분개 전표·사유 |

## 2. 권한 (RBAC)
- 조회/전기: `finance.periods` read/write (DIRECTOR/ADMIN). 일반 사용자 불가.

## 3. 통제 주체·주기
- 주체: 재무 결산 담당(ADMIN). 검토: 재무책임자(DIRECTOR).
- 주기: 월 결산 시. 충당률 재검토: 반기/연 1회 또는 손실경험 변동 시.

## 4. 구현 근거
- 권한 게이트 `finance/router.py`(allowance_preview/post `_require(..., "finance.periods")`).
- 멱등·감사 `allowance_service.post`·`_money_audit("ALLOWANCE_POST")`.
- 사전점검 `period_service.preclose_check`(ALLOWANCE_NOT_POSTED).
