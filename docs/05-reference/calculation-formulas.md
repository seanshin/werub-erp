# ERP 계산식 레퍼런스 (전 도메인)

> 전 도메인 계산식·라운딩·법적 근거·입력 데이터 출처의 단일 참조 문서. 코드가 SoT이며 본 문서는
> 파일:라인으로 코드를 가리킨다(수식 변경 시 본 문서 갱신). 2026-07-15 전수 검토(4갈래)로 작성.
> 검토 결과 결함은 §9에 등록하고 심각도순 보완(별도 릴리스).

## 1. HR — 4대보험·소득세 (`hr/statutory.py`)

### 1.0 공통 라운딩

| 함수 | 방식 | 용도 |
|---|---|---|
| `_trunc10(x)` | 10원 미만 버림(원단위 절사) | 4대보험료·소득세·지방소득세(법규 절사 부합) |
| `round(x)` | 원단위 반올림 | 퇴직금·연말정산 결정세액·연차수당·DC부담금·성과급 |

### 1.1 4대보험 (`insurance` / `employer_burden`)

- 연도별 요율 `RATES{year}` — 2025·2026 등재, **미등록 연도 = ValidationError**(무음 폴백 금지·갱신 강제).
- 국민연금: `trunc10(clamp(보수월액, 하한 39.0만, 상한 637.0만'26) × 4.5%)` · 보수≤0→0.
- 건강: `trunc10(보수 × 3.545%)` / 장기요양: `trunc10(건강보험료 × 12.95%)` / 고용(근로자): `trunc10(보수 × 0.9%)`.
- 일용(DAILY) 특례: NP·건강은 (월 8일↑ or 60h↑)일 때만. 고용은 일용 포함. 산재=사업주 전액.
- 사업주: NP/건강/요양 동률 + 고용 `ei_rate`(기본 1.15%) + 산재 `wc_rate`(기본 0.7%) — `PayrollSettings` DB 설정.

### 1.2 소득세

- **근로소득공제** `_EARNED_DEDUCTION`: 500만 70% / 1,500만 40% / 4,500만 15% / 1억 5% / 초과 2%(구간 누적).
- **세율** `_TAX_BRACKETS`: `과세표준×세율−누진공제`, 6%~45% 8구간.
- **근로소득세액공제**(§59): 산출세액≤130만→55%, 초과→71.5만+(초과)×30%. 한도: 총급여 ≤3,300만 74만 →
  ≤7,000만 max(74만−(g−3,300만)×0.8%, 66만) → ≤1.2억 max(66만−…×0.5, 50만) → 초과 max(50만−…×0.5, 20만).
- **자녀세액공제**: 1명 15만 / 2명 35만 / 3명↑ 35만+30만×(n−2).
- **월 간이세액** `estimate_monthly_income_tax`: 연환산(×12) → 근로소득공제 → 인적공제(부양×150만) →
  누진세 → 세액공제 차감 → `/12` → `trunc10`. 지방소득세 = `trunc10(소득세×10%)`.
- **상여세액**(§194) `bonus_income_tax`: `(A−C)×월수` 구조 — A=(월급+상여/월수) 간이세액, C=기납부(전달분 우선).
- **일용 원천징수**: `(일급−15만)×6%×(1−55%)`(실효 2.7%)·일세액<1,000원 소액부징수 0.
- **연말정산** `year_end_settlement`: 총급여→근로소득공제→과세표준(부양 max(n,1)×150만)→산출→세액공제→
  결정세액(round). 정산액 = 기납부 − 결정(+환급/−추납).

### 1.3 근태·급여 명세 (`hr/attendance.py`·`hr/payroll_rules.py`)

- 근무분 = `(clock_out−clock_in)초//60`(버림)·분 단위 저장. 야간 폴백 = `ci.hour≥22 or co.hour≤6`.
- 지각/조퇴 = 배정 시프트 시각 ± 유예 10분.
- 통상시급 분모 `209h`(주40h 월환산). **OT = ot_h × (기본급/209) × 1.5**(근기법 §56·일용/감시단속 제외).
- 당직 정액 10만/회(의사·전공의), 야간 정액 12,000/h(간호사), 자격·감시수당 정액 — `hr.pay_rate_settings` DB 우선.
- 무급휴직 일할 = `−(기본급/209×8)×일수`.
- 공제 = 4대보험 + 소득세(일용=분리과세·그 외=간이세액) + 지방소득세.

### 1.4 퇴직급여 (`hr/retirement_service.py`·`hr/offboarding_service.py`)

- 평균임금(근사) = 기준월 이하 최근 급여 EARNING 합(30일분 근사). 퇴직금 = `severance(avg, 입사, 기준일)`
  (근속<365일=0).
- DB 충당 전입 = 추계액 − 직전 누적(증분). DC 부담금 = `round(avg/12)`.
- 보험수리 DBO = `Σ 명목추계 × ((1+임금상승률)/(1+할인율))^잔여연수`(기본 4%/3%/정년60 — `ActuarialAssumption`
  DB 오버라이드). 재측정(가정차이) = OCI(3410/2120).
- 퇴직정산: 통상일급=`avg/209×8`, 연차수당=`round(잔여일수×통상일급)`, 충당상계 `offset=min(누적충당, 퇴직금)` →
  전표 2120(상계)+5320(부족분)+5310(연차) / 2110(총액).

### 1.5 성과급 (`hr/incentive_engine.py`·`incentive_service.py`)

- 산정: NET_COLLECTION `순수입×rate` / TARGET_EXCESS `max(순수입−목표,0)×rate` / DEPT_SHARE / RVU `points×unit`.
  `adjusted = min(max(raw, floor), cap)`.
- 원천징수: FLAT `amount×rate` 또는 COMBINED(상여세액 §194·기납부 차감). `withheld = clamp(0, ·, amount)`.
- GL: DR 5310 / CR 2150(원천징수) / CR 2110. 급여 GL의 gross는 INCENTIVE/INCTAX 제외(이중계상 방지).

### 1.6 급여 GL (`hr/employee_service.py PayrollService`)

- `gross=Σ EARNING(외부라인 제외)` / `withheld=Σ DEDUCTION` / `net=gross−withheld`.
- 전표 DR 5310(gross·cost_center=소속부서) / CR 2150 / CR 2110(net). net<0(이례) 시 2110 차변.
- 사업주부담(설정 ON): `employer_burden(gross)` → DR 5330 / CR 2110.

## 2. 재무·회계 (`finance/`)

> 공통 전제: 금액 컬럼 `Numeric(16,0)` 정수 저장. 전표 균형은 `VoucherService.create`가 `Decimal` 정확 비교
> (허용오차 0). 정상잔액 규약: ASSET·EXPENSE=차변(d−cr), LIABILITY·REVENUE·CAPITAL=대변(cr−d).
> 재무제표 집계는 `status IN (POSTED, REVERSED)`(역분개 상계 위해 REVERSED 포함 필수 — 불변식).

### 2.1 재무제표 집계 (`statements.py`)

- 손익: REVENUE=cr−d, EXPENSE=d−cr, net=ΣR−ΣE(`exclude_closing`으로 결산분개 제외).
  by_function: cogs/sga=`cost_function` 태깅, 영업이익=tr−cogs−sga.
- 의료기관 운영성과표: 의료이익=의료수익−의료비용, 비의료 항목은 계정 화이트리스트.
- 재무상태표: 자산=d−cr·부채=cr−d·retained=ΣREV−ΣEXP. kind 미매핑+잔액 계정은 `unmapped_accounts`로
  가시화하고 `balanced` 판정에서 차단(가짜 균형 방지).
- 자본변동표: closing=opening+change + 원장 차대 독립검산(`ledger_balanced`).

### 2.2 재무비율 (`financial_ratios` 6종)

`current_ratio`(유동자산/유동부채)·`debt_ratio`(총부채/자본)·`equity_ratio`(자본/총자산)·
`operating_margin`(net_income/revenue·"의료이익률 근사")·`labor_cost_ratio`(5310+5320/rev)·
`material_cost_ratio`(5410/rev). 유동 분류=`accounts.is_current`(G8) 우선→prefix 폴백. **분모 0 → None 일관**.

### 2.3 현금흐름표 (간접법+직접법)

- 항등식 기반: 비결산 전표는 전표별 차=대 ⇒ Σ비현금(cr−d)=ΔCash. 활동분류(`cf_activity`)는 split만 좌우,
  총합 불변 → `reconciled = opening+net_change==closing` 자가검증.
- 현금계정 = 활성 `CorporateAccount.gl_account_code` ∪ 1110(외화예금 누락 방지).

### 2.4 고정자산 (`asset_service.py`)

- 정액 월감가 `(취득−잔존)/(N×12)`·정률 `rate=(1−(salv/cost)^(1/N))/12, monthly=book×rate`(경과월 결정론).
- 처분: `_depreciate_through`로 처분일까지 감가 현행화(IAS 16.55) 후 `gain=proceeds−book`. 구성요소 선(先)
  정수화로 ±1원 균형실패 방지.
- 손상 `loss=book−회수가능액`(재평가잉여금 우선 차감·K-IFRS 1036)·환입은 원가한도·재평가 delta=fair−book.

### 2.5 리스 (K-IFRS 1116 `lease_service.py`)

- 개시부채 `PV=pmt×(1−(1+r)^−n)/r`(r=연이율/12·r≤0→pmt×n)·ROU=부채.
- 월 이자=`round(liab×r)`·원금=pmt−이자(최종기 true-up)·상각=`rou_carrying/잔여기간`(재측정 후 정액 유지).

### 2.6 대손 ECL (K-IFRS 1109 `allowance_service.py`)

`required = Σ(버킷잔액×버킷요율) × overlay(0.1~5.0)` → delta=required−현재 1190 잔액 → 5690/1190 전입·환입.
기본요율 current 0.5% ~ 90+ 50%(DB 오버라이드).

### 2.7 외화 (K-IFRS 1021 `forex_service.py`)

입출금 `krw=round(fc×rate)`. 기말 환산 delta=`round(fc잔액×기말환율)−장부가` → 4650(이익)/5670(손실). 멱등.

### 2.8 채권채무·자금 (`settlement_service.py`)

aging 버킷 = 경과일 ≤0/≤30/≤60/≤90/90+ (`reconstruct`=월말 GL 재구성). 자금수지 = 현금계정 DR=유입/CR=유출.
cash_forecast = 기초현금 + due_on 버킷별 미수/미지급 투영.

## 3. 재고평가·원가배부 (`scm/`·`costing/`)

- 재고평가 = Σ(잔량×단가)(단가 NULL=0 가시화). WAVG=`round(장부가/수량,2)`. NRV 저가법 loss=max(0, book−qty×nrv)
  → 5540/1159 결산분개. COGS=이동시점 확정 cost 합(WAVG는 출고 전 이동평균·FIFO는 lot 실단가 — `stock_service.issue`).
- 회전율 = 기간출고/기말재고(간이·수량기준), days=기간일/turns. dead stock = 재고>0·출고 0.
- ABC 배부: share=amount×(w/Σw). 동인 CASES/AREA 미입력 시 HEADCOUNT 폴백. **총액보존 검증**:
  `save_snapshot.reconciled = |grand−input| ≤ 부서수+1`.
- 다단계: 지원부서 원가를 진료부서 headcount 비례 2단 배부. 진료과 손익 = 수익−배부원가−삭감손실.

## 4. 환자 수납 (`patient/`)

- **본인부담 10원 절사**(건보 시행령 §19): `benefit_trunc=(int(급여 본인부담)//10)×10`(버림·비급여 미절사),
  보험자부담=총액−본인부담(절사분 보험자 흡수).
- 발생주의 delta: target−GL 1131 실제 전기합(미러 아님) → 1131/4110 델타 전기(단조 유니크 ref).

## 5. 의료보험 청구·규제 (`claims/`·`regulatory/`·`app/compliance_service.py`)

### 5.1 사전심사 룰엔진 (`claims/pre_review.py`)

- 산정횟수 정규화: `count None→1`, 0·음수 보존(취소·조정 라인) 후 `c<=0` 집계 제외 — 전 룰 일관.
- 산정횟수 상한: MAI1=라인 `count>max`, MAI2/3=동일(chart,code,day) `Σcount>max`(HIRA MUE).
- 금액이상: DRUG=`amt > cap×cnt×1.001`(0.1% 톨러런스)·그 외=`(amt−unit×cnt)/(unit×cnt) > tol(0.1)`
  — 상방 편차만(과소청구 자기손해 제외), expected 0 skip.
- 통계 이상: 선형보간 백분위(`(n−1)×pct/100` numpy linear 동일) × factor 임계, 단가=amt/cnt,
  `min_samples(10)` 미만 skip.
- 가산 적정성: 야간 `hour≥20 or <8`(KST 변환 후)·공휴=일요일+고정양력+EXTRA_HOLIDAYS·토요 옵트인.
- 기간 상한: 주=ISO 연도-주 버킷(연말 경계 정확)·월 버킷 `Σcount > max`.
- 위험액: 코드별 1회 + 무코드 적발 합(중복 룰 인플레 방지).

### 5.2 법정기한 (`claims/deadline.py`)

`base = 도착일+90일`(건보법 §87·민법 §157 초일불산입 — `+90`이 말일로 정확), 말일 토·공휴 익일 연쇄(§161),
처분일 있으면 `min(base, 처분일+180일)`. 공휴=토·일+고정양력+DB 캘린더(`common.holidays`)/코드 폴백.

### 5.3 지급대사·변동대가 (`claims/service.py`·`variable_consideration_service.py`)

- variance = 월결정액 − 누적지급. 과다지급 분리: `ar_credit=min(지급, 잔여 AR)`·초과=2141 선수금(음수 1140 방지).
- 변동대가(K-IFRS 1115): 삭감률=`clamp(Σ(청구−결정)/Σ청구, 0, 1)`(round 4자리·표본0→0) × 미결정 풀 →
  expected − 2155 대변잔액 = delta 전기(4110/2155).

### 5.4 간호등급 (`regulatory/nursing_grade.py`) — 전 항목 고시 일치 검증됨

- 산정창 = 전전분기 말월 15일~전분기 말월 14일(양단 포함 `+1`). 평균=Decimal ROUND_HALF_UP(0.01)·
  비율=ROUND_DOWN 절사(고시 라.4)·등급="미만" 경계. 단시간 FTE ≥40→1.0·[16,40) 0.4~0.9·<16→0.
- 간호사 0 → ratio −1·최저 등급(안전 방향).

### 5.5 방사선·폐기물·일마감·환불

- 방사선 연 한도: `ytd > 50 mSv` over / `≥40`(80%) near.
- 의료폐기물 보관기한: 손상성 30일·기본 15일(유형표) → 만기 D-day 밴드.
- 일마감 분개: 카드→DR 1132·그 외→DR 1110 / CR 4110(현금주의) 또는 1131(발생주의).
- 환불: `available = Σpaid − Σrefund` 상한 차단·care_month 스코프(접수월↔환불월 대칭).

### 5.6 청구 명세서 산정 엔진 (`claims/claim_calc.py` — D3-B1·회계 계산식)

진료일(`treated_on`) **시점 단가**로 계산 → 소급 재계산 안정(과거 명세 재산정 시 당시 FeeMaster·가산율로
동일 결과). EDI 포맷(`claims/edi_mapping.py`)과 **완전 분리** — 산정은 회계 계산, 매핑은 표현.

- **라인 금액** = `단가(as-of) × 산정횟수 × (1 + 종별가산율)`. 단가 = `FeeLookupService.get_price(code,
  code_type, at)`(temporal)·`at`=**라인별 진료일** 우선(다중 진료일 명세)·미지정 시 명세 기준일.
- **종별가산** = 활성 `InstitutionSurchargeRate[grade]`(없으면 0)·**[0,1] 클램프**(>100% 폭주 차단)·
  적용대상 = `applies_to` 콤마구분 집합(기본 TARIFF)·`quantize(1)` 원 절사. **비급여 라인 무가산**
  (급여 행위료 개념·`benefit_type=비급여`/NONCOVERED 제외). worklist `resolve_fee_with_mapping`과 동일 규약.
- **종별 본인부담률(rates)** = `patient/billing_rates.load_billing_rates(treated_on)`(InstitutionGradeSetting
  시점관리) 주입 — ⚠미주입 시 전 종별 병원 40% 오산정(의원30·종합50·상급60). grade는 명시 인자 우선,
  없으면 시점 설정값(billing 권위 소스)·surcharge와 동일 어휘(CLINIC/HOSPITAL/GENERAL/TERTIARY).
- **본인부담/공단부담/비급여 분리** = `billing_engine.compute_invoice(rates=, grade=)` 재사용(중복 로직 없음):
  급여 본인부담 10원 절사(건보 시행령 §19)·비급여 전액·절사분 보험자 흡수. `claim_amount = insurer_share`.
- **묶음(bundling)** = 명세에 패키지(parent) 존재 시 포함 세부/재료(child) **청구 제외**(`BundlingRule`
  parent_code→child_codes·0원 청구·`bundled_excluded` 노출).
- **미해소 코드** = 단가 조회 실패 → `unresolved_codes`·0원(합계 미반영·과다청구 방지).
- **실 데이터 배선** = `calc_for_chart(chart_no, care_month?, encounter_id?)` — 청구 라인 상세는
  ClaimStatement가 아닌 **`patient.BillingLine`**(HIS billing.line_recorded 미러)에 영속·취소 제외·라인별
  진료일 as-of. EP `GET /claims/calc/from-chart`. ⚠HIS 비표준코드는 drug 매핑 선적용 후 표준코드 기준
  해소(미매핑은 unresolved 가시화). 반환: `total`·`covered_total`·`claim_amount`·`patient_share`·
  `patient_share_raw`·`copay_rounding`·`non_covered`·라인별 `formula`·`formula_version`(회계 근거 추적).

### 5.7 EDI 매핑 엔진 (`claims/edi_mapping.py` — D3-B2 격리 시드·temporal 버전)

⚠ **격리·시드**: 세그먼트 구조는 실 심평원 포맷 아님(대표 필드)·각 버전 `production_ready=False`·제출 경로
미연결. 사용자 지침(포맷 교체 잦음·시간 역행 재계산 빈번) → **발효일 기반 temporal 버전 레지스트리**
(`EdiMappingVersion{version, effective_from, production_ready, mapper}`·현행 `SEED_v0`):
- **포맷 교체** = 새 버전 **append**(effective_from=고시 시행일)·과거 버전 엔트리 **불변**(FeeMaster
  valid_from/valid_to와 동일 시점 개념).
- **시간 역행 재계산** = `resolve_version(as_of)`가 청구/진료 시점 유효 버전 선택 — 2025 청구를 오늘
  재생성해도 2025 당시 포맷으로 매핑. `map_to_edi(calc, as_of=, version=)`: version 명시=고정, 아니면
  as_of→calc.treated_on→현행 최신 순 해소. 버전 메타(mapping_version·effective_from·_production_ready)는
  레지스트리가 SoT.
- 입력 = 산정 결과 dict(단가 as-of 반영)·출력은 표현만(산정과 완전 분리·포맷 변경이 산정 로직에 무영향).
- 실 스펙(B0) 확보 시 실 세그먼트 매퍼를 (effective_from=고시일·production_ready=True) 새 버전으로 등록하고
  `edifact.serialize`(아웃바운드)에 연결. 그 전까지 제출 무관.

## 6. 프론트 콤보박스·선택 데이터 감사 (2026-07-15 전수)

- **API 기반(안전)**: 진료과(/departments)·감사 action·법정교육 category·직원 픽커·계정과목 픽커(account-picker)·
  ITAM 계약/자산/부서 셀렉트.
- **하드코딩이나 백엔드 CHECK/서비스와 일치 확인(안전·26종)**: 간호 종별(4)·역할(7·WORKSTATION 의도 제외)·
  수납 method·재고 이동(4)·계정 kind(5)·전표규칙 line_type(8)·세금계산서 유형(3)·휴일 kind(4)·성과급 원천(3)·
  휴직 kind(5)·부양 relation(6)·incentive_type(5)·자산 category/상각(5/2)·ITAM status/계약/유지보수/서비스
  유형·notice_type(8)·사전심사 status/severity·학습 label/quality 등.
- **불일치·결함**: §9의 FE-1(수가조회 NONCOVERED 누락)·FE-2(날짜 기본값 UTC off-by-one)·FE-3(공동서명 채널
  옵션 비일관)·FE-4(CHECK 없는 자유문자열 컬럼은 UI가 유일 게이트 — 드리프트 주의).

## 9. 결함 등록부 (2026-07-15 전수 검토 · 4갈래)

> 심각도순 보완. 상태: ⬜ 미착수 / ✅ 수정(릴리스 표기) / 📋 문서화 수용(설계 결정).

### HIGH

| # | 위치 | 내용 | 상태 |
|---|---|---|---|
| H-1 | hr/statutory.py | 소득세 구간·근로소득공제·세액공제·인적공제가 연도 무관 하드코딩 — 세법 개정 시 무음 구식 세액(4대보험 RATES는 fail-safe 有·비대칭) | ✅ v1.135 |
| H-2 | hr/payroll_rules.py | 휴일근로수당(근기법 §56 휴일가산 8h내 50%) 라인 미구현 | ✅ v1.135 |
| H-3 | finance/settlement_service.py:265 | cash_forecast 기초현금이 POSTED만 집계(REVERSED 누락) — 역분개 존재 시 기초현금 왜곡 | ✅ v1.133 |
| H-4 | patient/service.py:412 | 환불 역전표가 method 무관 항상 CR 1110(현금) — 수납은 카드→1132 분리인데 환불 비대칭 → 현금 시재·카드미수 왜곡 | ✅ v1.133 |

### MEDIUM

| # | 위치 | 내용 | 상태 |
|---|---|---|---|
| M-1 | forex/allowance/valuation/disclosure 잔액쿼리 | 전표 status 미필터(DRAFT/REJECTED 수동전표가 환산·ECL·충당 오염 가능) — statements 규약(POSTED+REVERSED)과 불일치 | ✅ v1.133 |
| M-2 | finance/allowance_service.py:37 | ECL 요율 부분시드 시 `or _DEFAULT_RATES` 무력화 → 누락 버킷 요율 0(대손 과소) | ✅ v1.133 |
| M-3 | costing/service.py:214 | multistage 전 진료부서 headcount=0이면 지원원가 전량 소실(총액보존 위반·검증 부재) | ✅ v1.133 |
| M-4 | finance/statements.py:386 | operating_margin 분자가 net_income(순이익) — 라벨은 "의료이익률"(비영업·법인세 혼입) | ✅ v1.133 |
| M-5 | patient/service.py:291 | 일마감 net_cash=전 수단 합계인데 "현금 시재 대사" 라벨(카드 포함 오도) | ✅ v1.133 |
| M-6 | app/compliance_service.py:31 | 의료폐기물 격리 7일 누락(기본 15일 처리 → 경고 8일 지각) | ✅ v1.133 |
| M-7 | radiation·compliance | 방사선 5년 누적 100mSv 한도 미검증(연 50만 점검) | ✅ v1.133 |
| M-8 | claims/pre_review.py:245 | 공휴가산 판정이 코드 EXTRA_HOLIDAYS만(2026) — DB 공휴 캘린더 미주입(연도 갱신 누락 시 오탐) | ✅ v1.133 |
| M-9 | claims/worklist_service.py:249 | 위험액 by_code가 마지막 값 덮어쓰기 — 동일 코드 다중라인 과소집계 | ✅ v1.133 |
| M-10 | hr/incentive_service.py:106 | COMBINED 상여세액에 dependents 미전달(기본1) — A·C 기준 불일치로 과다 원천징수 가능 | ✅ v1.133 |
| M-11 | hr/payroll_rules.py | 야간수당 산정 — ✅ v1.137 설정화(night_pay_mode FLAT/STATUTORY/MAX·시드 FLAT·관리화면) |
| M-12 | hr/statutory.py | NP 상·하한 7월 갱신 — ✅ v1.136 연례 점검 만기 경보(컴플라이언스)+운영 절차 |
| M-13 | hr/retirement·offboarding | 평균임금 — ✅ v1.136 최근 3개월 EARNING 평균으로 정밀화(통상일급 분모는 명시 근사 유지) |
| FE-1 | regulatory/fees/page.tsx:34 | code_type 셀렉트에 NONCOVERED(비급여) 누락 — 비급여 코드 조회 불가 | ✅ v1.134 |
| FE-2 | 프론트 6+화면 | 날짜 기본값 `toISOString()`(UTC) — KST 00~09시에 "어제"로 채워짐(일마감·실사 일자 밀림) | ✅ v1.134 |

### LOW / 문서화 수용

| # | 내용 |
|---|---|
| L-1 | 전 도메인 Python round()=은행가반올림 — 정수 저장이라 실질 영향 미미, 정책 본 문서로 명시 |
| L-2 | 재고 회전율=기말재고 분모·DISPOSE 포함(간이 지표로 명시) |
| L-3 | 인적공제 하한 불일치(월추정 0 허용 vs 연말정산 max(n,1)) |
| L-4 | 자녀세액공제·일용 상수 → ✅ v1.135 TAX_PARAMS 연도화(H-1 포함) |
| L-5 | nursing truncate_ratio 미사용 중복 함수 |
| L-6 | claims/service.py date.today() 서버 로컬(KST 컨테이너 아님 시 경계 오차) |
| L-7 | incentive floor>cap 시 floored 플래그 오표기·실적 0에도 floor 지급 |
| FE-3 | 공동서명 채널 옵션 비일관 → ✅ v1.134 DIRECT 추가 |
| FE-4 | CHECK 없는 자유문자열 컬럼(ITAM category·EMP_TYPES 등)은 UI가 유일 게이트 |
| FE-5 | inputMode="numeric" 필드 다수 무검증(NaN 전송 가능) — 제출부 가드 권장 |
