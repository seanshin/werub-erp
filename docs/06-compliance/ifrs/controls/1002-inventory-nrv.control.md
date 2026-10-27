# 통제기술서 — 재고 저가법 NRV (K-IFRS 1002) · ICFR

**상태**: Draft · **작성일**: 2026-06-22

## 1. 통제활동
| # | 통제 | 유형 | 주기 | 증적 |
|---|---|---|---|---|
| C1 | NRV단가·원가흐름 설정은 scm.items write 권한자만 | 예방 | 변경 시 | items 변경·감사 |
| C2 | 저가법 전기는 미리보기 검토 후 전기(finance.periods) | 예방 | 월/연 결산 | preview·전표 |
| C3 | 전기 전표 멱등(기간별 1전표) | 예방 | 상시 | source_ref UNIQUE |
| C4 | 전기 시 감사로그(INVENTORY_NRV_POST) | 적발 | 전기 시 | audit_logs |
| C5 | 단가누락·장기재고(dead stock) 리포트로 진부재고 식별 | 적발 | 상시 | valuation·turnover |
| C6 | 정정은 역분개만 | 예방 | 필요 시 | 역분개 전표 |

## 2. 권한 (RBAC)
- NRV 설정: `scm.items` write. 저가법 미리보기/전기: `finance.periods` read/write(결산조정).

## 3. 통제 주체·주기
- NRV 입력: 자재/구매 담당. 전기: 재무 결산 담당(ADMIN). 검토: 재무책임자.
- 주기: 월/연 결산. NRV 재검토: 분기 또는 시장가 급변 시.

## 4. 구현 근거
- `scm/router.py`(items/nrv PATCH·nrv preview/post). 전기·감사 `valuation_service.nrv_post`.
