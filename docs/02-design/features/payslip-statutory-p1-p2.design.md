# 급여명세서 P1(계산근거) · P2(의료진 실적명세) — 상세 설계

> **범위**: [[payslip-statutory-and-medical]] 기획의 **P1**(계산방법 = 시행령 §27의2 5호 완전 부합)과
> **P2**(의료진 실적/인센티브 특화 명세). P0([[payslip-statutory-p0]]) 완료(카탈로그·지급일·한글명) 전제.
> **작성일** 2026-07-08 · **상태** 설계 · **선행** P0.

---

# P1 — 계산근거(`calc_basis`) → 계산방법 완전 부합

## 1. 문제

법정 필수 5호(근로시간 연동 지급항목의 **계산방법·근로시간수 포함**)는 금액만으로 불가. `payroll_rules.
build_payroll`이 `연장 = base/209 × 1.5 × 시간`을 계산하지만 **결과 금액만 라인화**돼 명세가 산식을 못 띄운다.

## 2. 모델

```
hr.payroll_lines  + calc_basis: JSONB | null     # 계산근거(표시 전용·회계 SoT는 amount)
                  + item_name: varchar(40) | null  # 확정시 한글명 스냅샷(카탈로그 변경 방어)
```

- `calc_basis`는 **표시 전용**. 회계 금액은 `amount`가 SoT(이중소스 금지·§기획 8). 없으면 계산식 행 생략
  (P0 과거 라인·소급 데이터 정직 보호).
- `item_name`은 확정 시점 한글명 스냅샷(카탈로그 개명 후에도 과거 명세 재현 일관). 렌더는 line.item_name
  → 없으면 카탈로그 → 없으면 코드.

## 3. `calc_basis` 스키마 (항목별)

지급(EARNING):

| code | calc_basis 예 | 렌더 문구 |
|------|---------------|-----------|
| BASE(월) | `{"type":"MONTHLY","base":2000000}` | (계산식 생략·정액) |
| BASE(일용) | `{"type":"DAILY","daily":120000,"days":15}` | 120,000 × 15일 |
| OT | `{"hours":10,"hourly":9569,"rate":1.5}` | 10h × 9,569 × 1.5 |
| HOLIDAY | `{"hours":8,"hourly":9569,"rate":1.5}` | 8h × 9,569 × 1.5 (§56·초과분 ×2.0·M1) |
| NIGHT_PREMIUM | `{"hours":20,"hourly":9569,"rate":0.5}` | 20h × 9,569 × 0.5 (법정 야간가산·M2) |
| SHIFT | `{"hours":20,"unit":12000}` | 20h × 12,000 (임의 교대수당·정액) |
| DUTY | `{"count":4,"unit":100000}` | 4회 × 100,000 |
| QUAL | `{"monthly":120000}` | (정액) |
| BONUS | `{"type":"MONTHLY","base":500000}` | (정액·통상임금 산입·M3) |
| UNPAID_LEAVE | `{"days":2,"daily":76555}` | −2일 × 76,555 |

> **M2**: 기존 `NIGHT`(정액 `hours×NIGHT_HOURLY`)은 법정 야간가산(§56 통상시급×0.5)과 임의 교대수당이
> 혼재. **NIGHT_PREMIUM(통상시급×0.5)** 과 **SHIFT(정액)** 로 분리해 법정 산식과 명세 계산식을 일치.
> **M1/M3**: HOLIDAY(휴일가산)·BONUS(정기상여) 규칙을 `build_payroll`에 신설(현재 미생성).

공제(DEDUCTION) — `statutory.insurance`가 항목별 기준액·요율 반환하도록 확장:

| code | calc_basis 예 | 렌더 |
|------|---------------|------|
| NP | `{"base":3200000,"rate":0.045}` | 3,200,000 × 4.5% |
| HI | `{"base":3200000,"rate":0.03545}` | × 3.545% |
| LTC | `{"base":113440,"rate":0.1295}` | 건강보험료 × 12.95% |
| EI | `{"base":3200000,"rate":0.009}` | × 0.9% |
| TAX | `{"method":"간이세액표","dependents":1}` | 간이세액표(부양 1) |
| LOCALTAX | `{"base":31000,"rate":0.1}` | 소득세 × 10% |

> 통상시급 `hourly`는 통상임금 구성(2024.12 대법 판례·정기상여 포함·§기획 9)을 노무 검토로 확정한 값.
> **통상임금 산정을 단일 함수로 통일**해 OT·HOLIDAY·NIGHT_PREMIUM·UNPAID_LEAVE가 **같은 통상시급**을
> 공유(현재 OT만 base/209 — 야간·무급공제에도 동일 적용·M3). `calc_basis`가 그 값을 담아 계산식·회계 일치.
> **M5**: `base`는 명세에 인쇄되므로 **계산에 실제 쓰인 기준액**(4대보험은 코드가 당월 gross 사용)을 담고
> 라벨을 그에 맞춘다 — "보수월액"으로 오인쇄 금지(보수월액은 연 1회 신고 고정치라 별도 보유 시에만 사용).

## 4. 주입 지점 (순수성 유지)

- `payroll_rules._earn/_ded`에 3번째 인자 `basis: dict|None` 추가 → `build_payroll`이 각 라인 계산 시
  근거 dict 동봉(OT·HOLIDAY·NIGHT_PREMIUM·SHIFT·DUTY·UNPAID_LEAVE·BASE). 순수 함수 유지(회계 금액 불변).
- `statutory.insurance` 반환을 `{code: {"amount":…, "base":…, "rate":…}}` 형태로 확장(현 금액-only에서
  근거 포함). `build_payroll`이 이를 `_ded` basis로 전달.
- 영속: `employee_service`(build_payroll 라인 루프)·`incentive_service._add_payroll_line`가 `calc_basis`·
  `item_name`도 저장.
- **후방 안전**: basis 미동봉 라인은 `calc_basis=NULL`(정상). 렌더가 계산식 행만 생략.

## 5. 렌더 (payslip.html·P0 자리 활용)

```
연장근로수당      143,535
  └ 10h × 9,569 × 1.5                    ← calc_basis 있을 때만
국민연금          144,000
  └ 3,200,000 × 4.5%
```

- 각 라인 하위에 회색 소자 계산식 행. `calc_basis` 없으면 미출력.
- 일용 간이양식은 계산식 생략(P0 분기 유지).

## 6. 완료 기준 (P1)

- [ ] 연장·야간·당직·무급공제·4대보험에 **근로시간수/기준액·요율 포함 계산식** 표기.
- [ ] `amount`는 불변(회계 SoT)·`calc_basis`는 표시 전용(합계·전표 영향 0).
- [ ] `item_name` 스냅샷으로 과거 명세 재현 일관.
- [ ] 근거 없는 과거 확정분은 계산식 생략(허위 없음).

---

# P2 — 의료진 실적/인센티브 명세

## 1. 소스 (이미 존재)

- `hr.IncentiveCalculation`(`models.py:300`) — **`net_revenue`·`amount` 컬럼 + `snapshot`(JSON)** 를 보존
  (승인 시 `PayrollLine(INCENTIVE)` 생성, `incentive_service._add_payroll_line`). `snapshot` 키 =
  `{incentive_type, rate_pct, net_revenue, raw_amount, revenue}`(`incentive_service.py:60~61`) — **계약형·
  배분율은 IncentiveCalculation 컬럼이 아니라 snapshot 안 또는 계약 마스터에 있음**.
- 계약 마스터: `hr.MedicalStaffContract`(`models.py:257`·테이블 medical_contracts) — `incentive_type`·
  `incentive_rate`(배분율%) 컬럼 보유.
- `incentive_engine.calculate`(`incentive_engine.py`) 반환 `{incentive_type, rate_pct, net_revenue,
  raw_amount, amount, capped, floored}` → snapshot 원천.
- `physician_revenue.aggregate`(`physician_revenue.py`) — 담당의별 수납(현금)·청구(발생)·삭감·**net =
  진료수익 − 직접원가 − 삭감**. 진료수익은 **청구(발생)와 수납(현금) 중 택일**(claim 있으면 발생, 없으면
  수납·`physician_revenue.py:57~58`) — 합산 아님.
- 계약형: `incentive_type ∈ {NONE, NET_COLLECTION, TARGET_EXCESS, DEPT_SHARE, RVU}`(모델 CHECK
  `models.py:265`·5값·RVU=상대가치점수×단가 포함).

**핵심**: P2 렌더 자체는 **기존 `IncentiveCalculation.snapshot`을 명세에 표기**(snapshot→line.calc_basis 승격)
하는 것이 주다. 단, 성과급이 별도 경로로 생성되므로 아래 **통합 정합(H1~H3)** 을 먼저 해결해야 명세가 법적·
회계적으로 정확하다 — "snapshot 렌더만"으로는 불충분.

## 1.5 성과급 통합 정합 (H1~H3) ★ 선결

성과급(INCENTIVE)은 `build_payroll`(BASE+수당)과 **별개로** `incentive_service`가 라인·전표를 만든다. 이 분리가
명세에 세 결함을 유발:

- **H1 세무 base 누락**: `build_payroll`의 `gross`는 **INCENTIVE 미포함** → 그 gross로 계산한 4대보험·소득세
  `calc_basis`가 성과급을 뺀 값. 명세에 "지급총액(성과급 포함)"과 "공제 계산근거(성과급 제외)"가 **내부 모순**
  이고 원천징수 과소. **해결**: 성과급 확정 시 gross에 INCENTIVE 합산해 세액 재산정(상여 세액 방식)하거나,
  최소한 발행 게이트에서 `TAX base ≠ 지급총액` 검출·차단.
- **H2 재생성 시 유실**: `employee_service`가 해당 월 `PayrollLine` **전량 delete 후 재삽입**하는데 build_payroll은
  INCENTIVE를 안 만들어 **승인된 성과급 라인이 사라지고** 미지급 전표만 남아 고아화. **해결**: 재생성 시 승인된
  `IncentiveCalculation` 조회해 INCENTIVE 라인 재구성(또는 delete 범위에서 INCENTIVE 제외)·순서 보장.
- **H3 대사 오탐**: 회계 정합(§8)의 "명세 합계 ↔ 급여 전표"는 성과급이 **별도 미지급 전표**(급여비용/미지급금)
  이고 공제는 근로자부담만인데 전표엔 사업주부담·예수금이 섞여 **정상인데 상시 불일치**. **해결**: 대사 대상 =
  급여 voucher + 당월 승인 incentive voucher 합산, 사업주 법정부담분 제외·근로자부담 예수금만 매칭.

## 2. 의료진 명세 블록

일반 지급/공제 표 + **의료진 실적 배분 섹션**(is_medical·INCENTIVE 라인 존재 시):

```
─────────── 진료 실적 배분 (2026-06) ───────────
계약형          실적배분제(NET_COLLECTION)
배분율          15%
진료수익        45,000,000   (basis: 청구·발생주의)   ← 청구/수납 택일
삭감            (2,100,000)
직접원가        (8,000,000)
─────────────────────────────
순수입(net)     34,900,000
성과급 = 순수입 × 15% = 5,235,000        → 지급 INCENTIVE 라인과 일치
```

- 값은 `IncentiveCalculation.snapshot`에서 렌더(재계산 아님 — 승인 시점 확정치 재현).
- 산식 = `calculate`의 type별 문구(NET_COLLECTION="순수입×배분율", TARGET_EXCESS="max(순수입−목표,0)×율",
  DEPT_SHARE="진료과 순이익×기여율").
- 당직·교대 수당(DUTY/NIGHT)은 P1 `calc_basis`로 이미 표기(횟수·단가).

## 3. 개인정보 · 접근

- 진료실적·배분은 **본인 명세에만** 노출(본인 셀프서비스 경로 `_self_employee_q`). 부서장은 차단.
- **M7 — 일괄발행 권한 경계**: `hr.payroll` write 관리자가 전원 PDF를 생성하면 실적 섹션까지 열람 가능해
  "본인만"이 관리자에는 성립하지 않는다. 게다가 [[physician-incentive-contract]] §13은 성과급을 **경영·기획
  전용 RBAC**로 규정 — payslip이 일반 hr.payroll로 노출하면 민감정보 노출면이 넓어진다. **해결**: ① 실적
  섹션 포함 일괄발행은 별도 상위권한으로 게이팅하거나 ② 일괄발행 PDF는 실적 섹션 제외(기본 급여만)하고
  실적 상세는 **본인 셀프서비스 열람/링크(§기획 7.3)에서만** 렌더.
- snapshot에 환자·건별 식별정보 미포함(집계치만) — 최소필요 원칙([[erp-prereview-min-necessary]] 정합).

## 4. 계약형 헤더 표기

- 연봉제(고정 월할): 실적 섹션 없음·BASE 위주.
- 네트제(공제 후 보장): 보장액·실적 대비 표기(MG 미달 시 보전 라인).
- 실적배분제: §2 블록.
- 계약형은 `MedicalStaffContract.incentive_type`으로 분기(없으면 일반직 명세).

## 5. 완료 기준 (P2)

- [ ] 의료진 명세에 계약형·배분율·진료수익/삭감/원가·net·성과급 산식이 표기(snapshot 재현).
- [ ] 성과급 표기액 = `PayrollLine(INCENTIVE).amount`(이중계산 없음).
- [ ] 실적 섹션은 **본인 문서에만**(일괄발행 포함) 노출·타인 차단.
- [ ] 계약형별 레이아웃 분기(연봉/네트/실적배분)·미계약자는 일반 명세.

## 6. 단계 관계

```
P0 카탈로그·지급일·한글명 ─▶ P1 calc_basis 계산식(일반+INCENTIVE) ─▶ P2 의료진 실적 블록(snapshot 렌더)
                                     └ 회계 정합: 모든 amount = 전표 SoT (표시근거와 분리)
```

P2는 P1의 `calc_basis` 경로를 재사용하므로 **모델 신규 없음**(INCENTIVE snapshot→calc_basis 매핑 + 렌더
분기만). 의료진 실적 계산·계약 자체는 [[physician-incentive-contract]]·기존 엔진 소관.
