# 통제기술서 — 이연법인세 (K-IFRS 1012) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

## 1. 통제활동
| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | 유효세율 설정은 finance.periods write 권한자만·시점관리 | 예방 | 변경 시 | tax_rate_settings·감사로그 |
| C2 | 이연법인세 전기는 미리보기 검토 후 연말 전기 | 예방 | 연 1회 | preview 응답·전표 |
| C3 | 전기 전표 멱등(연도별 1전표) | 예방 | 상시 | source_ref UNIQUE |
| C4 | 전기 시 감사로그(DEFERRED_TAX_POST, 연도·DTA·DTL) | 적발 | 전기 시 | audit_logs |
| C5 | 연말 사전점검 '이연법인세 미전기' 경고 | 적발 | 연말결산 | preclose_check(12월) |
| C6 | 정정은 역분개만 | 예방 | 필요 시 | 역분개 전표 |
| C7 | 세무 전문가 검토(외부) — 세율·실현가능성·세무조정 | 예방 | 연 1회 | 검토 의견서(외부) |

## 2. 권한 (RBAC)
- 조회/전기: `finance.periods` read/write (DIRECTOR/ADMIN).

## 3. 통제 주체·주기
- 주체: 재무 결산 담당(ADMIN)·재무책임자(DIRECTOR) 검토 + **세무 전문가 외부 검증**.
- 주기: 연말 결산. 세율 재검토: 세법 개정 시.

## 4. 구현 근거
- 권한 `finance/router.py`(deferred_tax preview/post). 멱등·감사 `deferred_tax_service.post`·
  `_money_audit("DEFERRED_TAX_POST")`. 사전점검 `period_service.preclose_check`(DEFERRED_TAX_NOT_POSTED, 12월).
