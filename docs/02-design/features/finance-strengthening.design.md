# 재무회계 강화 설계서 (P0 — 완결성·내부통제)

> **범위**: 기획서 finance-strengthening.plan.md의 **P0 3항목** 설계 — ① 현금흐름표(간접법)
> ② 전표 승인 워크플로(maker-checker) ③ 결산 마감 체크리스트 + 확정 스냅샷.
> **원칙**: 기존 `finance/statements.py`·`service.py`·`period_service.py` 재사용. 신규 마이그 최소(②③).
> **작성일**: 2026-06-15 · **버전**: v0.1 · **상태**: 설계 · **연관 Plan**: finance-strengthening.plan.md

---

## 1. 공통 전제 (코드 근거)

- 전표 정합: `VoucherLine` 차/대 집계, `StatementService._by_account(since/until/exclude_closing)`가
  계정 kind(ASSET/LIABILITY/REVENUE/EXPENSE/CAPITAL)별 차·대 합 반환.
- 정상잔액: ASSET·EXPENSE=차변, LIABILITY·REVENUE·CAPITAL=대변. 역분개는 원+반대 라인 상계(status 무관).
- 손익은 `source_module != "closing"`(결산분개 제외)로 기간귀속 보존. 재무상태표는 포함.
- 현금 계정: `ACC_CASH`(1110, settlement_service 기준). 재무상태표 ASSET에 현금성 계정 존재.
- balanced 자가검증 패턴: `round(a) == round(b)` 정수 비교(Numeric scale 0).

---

## 2. ① 현금흐름표 (간접법) — `statements.cash_flow_statement`

### 2.1 산식 (간접법)

```
영업활동 현금흐름 =
    당기순이익(income_statement.net_income, 결산제외)
  + 비현금비용 가산 (감가상각비·손상차손·퇴직급여충당 등 EXPENSE 중 비현금)
  ± 운전자본 증감 (기초→기말 잔액차)
       − 매출채권 증가 / + 감소
       − 재고자산 증가 / + 감소
       + 매입채무 증가 / − 감소

투자활동 현금흐름 = − 유형자산 취득 + 처분 (자산계정 현금 상대분)
재무활동 현금흐름 = + 차입 − 상환 ± 자본 거래

기말현금 = 기초현금 + 영업 + 투자 + 재무
검증: 기말현금 == 재무상태표(as_of=기말) 현금성 계정 합  (자가검증 — 불일치 시 경고)
```

### 2.2 계정 → 활동 분류 (매핑)

- 신규 상수 `CASHFLOW_ACTIVITY: dict[account_code|kind, "OPERATING"|"INVESTING"|"FINANCING"|"NONCASH"]`
  (코드 시드 — 회계 담당 검토 §결정필요). 기본 규칙:
  - 운전자본(영업): 매출채권 1130·재고 1150·매입채무 등 → OPERATING(증감 반영)
  - 비현금 가산: 감가상각비(5210)·손상차손(5620)·퇴직급여충당(5320) → NONCASH(영업 가산)
  - 투자: 유형자산 1710/1810·리스 사용권 → INVESTING
  - 재무: 차입금·자본(CAPITAL) → FINANCING
- 미매핑 계정은 OPERATING 기타로 폴백 + 경고(분류 누락 가시화).

### 2.3 구현

- `StatementService.cash_flow_statement(session, *, fiscal_year 또는 since/until) -> dict`
  - `income_statement`로 net_income, `_by_account(until=기말)`·`_by_account(until=기초)`로 잔액차.
  - `balance_sheet`의 현금 계정으로 기초/기말 현금·정합검증.
  - 반환: `{operating:{net_income, noncash[], working_capital[], subtotal}, investing[], financing[],
    opening_cash, closing_cash, net_change, reconciled: bool}`.
- 라우터: `GET /finance/statements/cash-flow-statement?fiscal_year=` + `.xlsx`.
  (기존 `GET /cash-flow`[자금수지]는 **자금일보**로 유지 — 명칭 분리, 혼동 방지.)
- 화면: `/erp/finance-statements` 탭 추가.

### 2.4 테스트
- 시드 전표(매출채권↑·감가·자산취득·차입)로 영업/투자/재무 분류 검증.
- **reconciled 자가검증**: closing_cash == 재무상태표 현금 (핵심 — 기존 balanced 패턴).
- 결산분개 제외 일관성(net_income은 손익과 동일).

---

## 3. ② 전표 승인 워크플로 (maker-checker) — `service.py`

### 3.1 상태기계

```
DRAFT ─submit→ PENDING ─approve→ POSTED ─reverse→ REVERSED
                  └─reject→ REJECTED ─resubmit→ PENDING
자동생성(F1) 전표: 생성 즉시 POSTED (원천 통제 존재 — 워크플로 우회)
```

- `Voucher.status` CHECK 확장: `DRAFT|PENDING|POSTED|REJECTED|REVERSED` (마이그 1건, `ADD ... IF` +
  CHECK 재생성 — **prod 실제 제약명 확인 후 IF EXISTS** 교훈 적용[erp-prod-deploy 0061 사례]).
- 신규 컬럼: `submitted_by/at`, `approved_by/at`, `reject_reason`(nullable).

### 3.2 적용 범위 (선별 — 과도 통제 회피)

| 전표 유형 | 워크플로 | 근거 |
|-----------|----------|------|
| 자동생성(patient/claims/scm/closing 자동) | 즉시 POSTED | 원천 모듈 통제·멱등 |
| **수동 전표(manual)** | DRAFT→승인 | 사람 입력 — 통제 필요 |
| **역분개(reverse)** | 승인 필요(옵트인 정책) | 정정은 민감 |
| 임계금액 초과 | 승인 필요(정책값 `voucher_approval_threshold`) | 고액 통제 |

- **SoD**: `submitted_by != approved_by` 강제(ValidationError). 자기승인 차단.
- 감사: `record_audit(action="VOUCHER_APPROVE"|"VOUCHER_REJECT")` — 해시체인.
- 알림: 승인 대기 시 재무 MANAGER에게 `notifications`.

### 3.3 집계 정합 (핵심 안전경계)

- **재무제표·마감·예산집행은 `status == "POSTED"`만 집계**(DRAFT/PENDING/REJECTED 제외).
  → `_by_account` 등 집계 쿼리에 `Voucher.status == "POSTED"` 조건 추가.
  ※ 현재는 status 무관 전 라인 집계(역분개 상계 전제) — REVERSED는 원+반대 상계라 포함 유지,
    DRAFT/PENDING/REJECTED만 신규 제외. **회귀 주의**: 기존 테스트가 자동 POSTED라 영향 적으나 전수 확인.

### 3.4 API / 테스트
- `POST /finance/vouchers`(manual)→DRAFT, `POST /vouchers/{id}/submit|approve|reject`.
- 테스트: 상태전이·SoD 위반 차단·미승인 집계제외·임계금액·자동전표 우회·감사기록.

---

## 4. ③ 결산 마감 체크리스트 + 확정 스냅샷 — `period_service.py`

### 4.1 마감 전 점검 (`preclose_check`)

`POST /finance/periods/{yyyymm}/preclose-check` → 경고 목록(차단 아님, 기본):

| 점검 | 규칙 |
|------|------|
| 미결 전표 | 해당월 DRAFT/PENDING 전표 존재 |
| 감가 미계상 | 활성 고정자산 중 당월 depreciation_entries 없음 |
| 미배부 원가 | 당월 ABC 배부 스냅샷 부재(costing) |
| 차대 불일치 | 당월 시산표 balanced=false |
| 미매칭 세금계산서 | 당월 tax_invoices 미매칭 |
| 기간귀속 | 마감월 이후 voucher_on이 해당월로 들어온 건 |

- `close(yyyymm)` 시 옵션 `enforce=True`면 점검 실패 시 차단(정책값). 기본 경고.

### 4.2 확정 스냅샷

- 마감 확정 시 재무제표 4종(시산표·손익·재무상태표·현금흐름표) JSON 스냅샷 저장.
- 신규 테이블 `finance.closing_snapshots(yyyymm, kind, payload JSONB, created_by/at)` (마이그 1건).
- 사후 변경 감지: 재마감/재오픈 시 스냅샷 보존(이력) — 무단변경 감사.

### 4.3 테스트
- 점검 규칙별 양/음성, enforce 차단, 스냅샷 저장·조회.

---

## 5. 마이그레이션 / 영향

| 마이그 | 내용 | 위험 |
|--------|------|------|
| `00XX_voucher_approval` | voucher status CHECK 확장 + submitted/approved/reject 컬럼 | CHECK 제약명 prod 확인(IF EXISTS) |
| `00XX_closing_snapshots` | finance.closing_snapshots 테이블 | 신규 — 저위험 |
| (현금흐름표) | 마이그 없음(집계 로직) | — |

- 집계에 `status="POSTED"` 추가는 **동작 변경** — 전수 테스트·프로드 검증 필수.
- core 컨테이너 부팅 시 alembic 자동 적용(기존 운영 사실).

---

## 6. 구현 순서 (M1→M3)

1. **M1 현금흐름표** — 마이그 0, 데이터 자족, 4대제표 완성. **최우선(최고 ROI)**.
2. **M2 전표 승인** — 상태기계·SoD·집계 status 필터·마이그.
3. **M3 마감 체크리스트 + 스냅샷** — 점검 규칙·스냅샷 테이블.

각 단계 PR→CI→머지→배포(기존 흐름). 정합 자가검증 테스트 필수.

---

## 7. 결정 필요 (협의 — Plan §11 연계)

- [ ] 현금흐름 **활동분류 매핑** 초안 검토 — 회계 담당
- [ ] 전표 승인 **대상**(수동만 vs 역분개 포함 vs 금액초과) + **임계금액** — 재무팀
- [ ] 마감 점검 **경고 vs 차단(enforce)** — 재무팀

> 기본값(수동+역분개 승인, 마감 경고, 미매핑 OPERATING 폴백)으로 dev 선행 가능 — 확정 시 정책값만 조정.
