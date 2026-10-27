# 사전심사(Claims Pre-Review) 강화 기획서

> **작성일**: 2026-06-23 · **버전 v0.2**(코드 검증 기반 상세화) · **상태**: 기획(Plan)
> **연관**: 사전심사 FR-41/PR-A·심사보조(#319)·의료진 게이트(#315)·학습사례(#316/#317)·자동화 파이프라인([[erp-claims-auto-review]])
> **범위**: services/core/src/erp/claims/ (pre_review·worklist·learning) + apps/web/.../claims/pre-review
> **전제(법적 경계)**: 본 강화는 **행정적 청구오류·산정기준 점검**에 한정. 임상 의학판단은 의료법 제27조에 따라 **의료진 확인 게이트**를 유지(자동 의학판단 금지).

## 1. 배경·목적
현행 사전심사는 정형 룰 10종 + 워크리스트 + 의료진 협업 + 학습사례 + AI 보조까지 갖춘 견고한 기반이다(§2). HIRA 심사기준·국제 청구심사 표준(NCCI 등)과 대조 시 **묶음청구·진단-수가 연계·약제 DUR·재료대 별도산정·고시 버저닝·통계 이상탐지** 등 공백이 있다. v0.2는 코드 검증으로 As-Is·선결조건·상세설계를 구현 착수 가능 수준으로 확정한다.

## 2. 현행 구현 (As-Is, 코드 검증)
### 2.1 룰 엔진 아키텍처 — `claims/pre_review.py`
- **순수 평가함수 + 디스패치**: 각 룰 = `fn(lines: list[dict], rule, fee: dict) -> list[Finding]`. `_EVALUATORS = {rule_type: fn}` 매핑. `PreReviewEngine.evaluate(lines, rules, fee)`가 활성 룰만 순회.
- **DB 주입식**: 룰은 `claims.pre_review_rules`(rule_type·code·name·severity·`params` JSONB·active). 엔진은 DB 비의존(라우터가 활성룰·수가맵 주입) → **룰 파라미터·활성화는 코드 수정 없이 DB로**, 신규 룰타입만 평가함수 등록 필요.
- **Finding** dataclass: `rule_code·rule_name·severity·chart_no·code·detail·treated_on`. ⚠ **거부사유 코드 필드 없음**(P0-2 추가 대상).
- **급여 한정**(`_BENEFIT_ONLY_RULES`): 비급여·자격상실(EXPIRED/NOT_FOUND) 라인은 급여 삭감룰 제외, NONCOVERED_CLAIM만 비급여 포함.

### 2.2 구현된 룰 10종
중복·산정횟수(약제·재료 투여량 제외)·기간상한·가산적정성·LOS·단가이상치·배치내분포·미등재(BLOCK)·동시산정불가(BLOCK)·성연령(BLOCK·템플릿비활성)·비급여오청구(BLOCK).

### 2.3 엔진 입력 데이터 가용성 (검증)
`patient.BillingLine` 보유 컬럼: chart_no·code·code_type·count·treated_on·treated_at·**patient_age·patient_sex·diagnosis_codes**·special_case_type(산정특례)·medical_aid_class·coverage·unit_price_his·admitted_on·encounter_type·dept_code.
- ✅ **성·연령(patient_age/sex)·상병(diagnosis_codes) 이미 존재** → G2·G5 룰의 데이터 토대 확보.
- ⚠ 현행 **엔진 입력 line dict가 patient_age/sex/diagnosis_codes를 전달하는지 미확인** → 선결조건 §4.
- ⚠ `regulatory.DrugCodeMap`(his_code·insurance_code·drug_name)에 **성분(ingredient/ATC) 컬럼 없음** → DUR(G3)의 핵심 선결조건.

### 2.4 기반 기능
약품매핑 단가해석·상태머신(OPEN→IN_REVIEW→BLOCKED/CLEARED/FIX_REQUESTED→RESOLVED)·자동 이상없음(P1)·자동 오탐(P2 캘리브레이션)·의료진 질의-회신-종결(physician_gated)·학습사례 자동수확/JSONL·AI 보조(weve)·부서 IDOR·감사.

## 3. 빠진/약한 영역 (Gap) — 우선순위·난이도 재평가
| # | 영역 | 데이터 토대 | 난이도 | 우선 |
|---|---|---|---|:--:|
| G3 | **약제 DUR**(병용·연령·임부 금기, 동일성분 중복) | ⚠ 성분 매핑 부재 | 중(선결조건 有) | 🔴 |
| G8 | **거부사유 코드화**(CARC/RARC식) | finding 필드 추가 | 하 | 🔴 |
| G2 | **진단(KCD)-수가 연계**(형식: 상병미기재·성/연령 모순) | ✅ diagnosis_codes 有 | 중 | 🔴 |
| G1 | **묶음청구(Bundling/Unbundling)** | 신규 룰테이블 | 중 | 🟡 |
| G5 | **성·연령 적합성 자동화** | ✅ patient_age/sex 有 | 하 | 🟡 |
| G4 | **재료대 별도산정** | 신규 룰테이블 | 중 | 🟡 |
| G7 | **고시 버저닝·효력일 자동적용** | rules 컬럼 추가 | 하 | 🟡 |
| G6 | **종별가산 재검증** | institution_grade 有 | 중 | 🟡 |
| G12 | **룰엔진 일반화**(선언적) | — | 중 | 🟡 |
| G9 | **통계 이상탐지**(의료진·부서·환자·시계열) | — | 중 | 🟢 |
| G10 | **삭감 예측(ML)** | 학습사례 有 | 상 | 🟢 |
| G11 | **자격 검증 심화** | — | 중 | 🟢 |

## 4. 구현 선결조건 (Foundational — P0 착수 전)
1. **엔진 입력 line dict 강화**: `worklist_service.sync_from_billing_lines`가 BillingLine→engine line dict 구성 시 **patient_age·patient_sex·diagnosis_codes·special_case_type**를 포함하도록 확인·보강. (없으면 G2/G5/DUR 연령금기 작동 불가.) 영향: 단일 함수, 저위험.
2. **약품 성분(ingredient) 매핑**: DUR은 **성분 단위**(병용금기 약물쌍=성분쌍). 경로 옵션:
   - (a) `DrugCodeMap`에 `ingredient_code`(주성분) 컬럼 추가 + 약가마스터/식약처 매핑.
   - (b) 별도 `claims.drug_ingredient_map`(insurance_code→ingredient_code[]) 신설.
   - 권장 (b): 보험코드↔성분 N:M, 공개 DUR 데이터의 성분코드 체계와 정합. HIS/약가마스터에서 성분 수신 협의(D-DUR).
3. **거부사유 코드 체계 정의**: 한국 청구오류 유형(A 금액산정착오·F 증빙미제출·K 코드착오/누락·L 수탁기관기호착오) + 내부 세분(룰별) 매핑표 확정.

## 5. 참조 문헌·표준 (조사)
### 5.1 한국 HIRA
- 법령: 요양급여 기준 규칙[별표1]→복지부 고시 세부사항. 청구방법·작성요령 별도 고시. HIRA 심사지침 통합본(2024.7판, repository.hira.or.kr). 조회 biz.hira.or.kr·rulesvc.hira.or.kr.
- 프로세스: 접수→전산점검→전산심사→전문심사→사후관리. 명시 자동점검 카테고리: **최대산정횟수·약제/DUR 전산심사 적용기준**.
- **DUR 4종**: 병용·연령·임부 금기 + 동일성분 중복. **공개데이터**: 병용금기 약물쌍(성분명·제품명·금기사유·공고번호, data.go.kr/15089525)·식약처 DUR OpenAPI(15059486). 라이선스 유형2(출처표시·비상업).
- 청구오류 유형코드 A/F/K/L. 공개심의사례·「요양급여 청구 부당사례 모음집」. 코드: 진단 KCD·수가/약 HIRA EDI(~84,000)·EDI 99%.
### 5.2 국제 claim edits(설계 템플릿)
- CMS NCCI: PTP(조합 차단·수정자지표 0/1/9)·**MUE(산정횟수 상한·MAI 1/2/3)**·Add-on. NCD/LCD medical necessity=진단-시술 연계.
- claim scrubbing 7범주: 번들·수정자·성연령·진단-수가·중복·빈도·장소.
- 표준: X12 837/**277CA**(adjudication 이전 접수상태)·**CARC/RARC**(거부사유 코드·설명가능성). FHIR Claim·Da Vinci PAS 차세대.
### 5.3 AI/통계(검증)
이상탐지=비지도(Isolation Forest·autoencoder)·지도(XGBoost+불균형)·GNN(담합). 삭감예측=GBM(지급자 implicit rule 학습, 제출 전 스코어). 휴먼인더루프·설명가능성 필수.

## 6. 오픈소스·공개데이터 (라이선스·적용성)
| 항목 | 라이선스 | 적용성 |
|---|---|---|
| **현행 자체 엔진**(pure-fn + JSONB) | — | ✅ **P0/P1은 현행 패턴 확장 권장**(평가함수 등록 + DB 파라미터). 추가 의존성 0 |
| rule-engine(zeroSteiner) | BSD-3 | △ P1+ 선언적 표현식이 늘면 검토 |
| GoRules ZEN | MIT | △ 결정테이블 UI 필요 시 P2 |
| openIMIS "Claims Review Engine" | AGPL-3.0 | ⚠ 참조 아키텍처만(소스공개 의무) |
| **DUR 공개데이터** | 유형2 | ✅ 직접 적용(병용/연령/임부/중복) |
| CMS NCCI·CPT | AMA | ✗ 미국 전용(분류만 차용) |
| RxNorm/ATC/KD-code | 다양 | △ 성분 매핑 보조 — KCD/HIRA 기준 |

**결정(G12)**: 현행 순수함수+JSONB 엔진이 이미 확장성·테스트성 우수 → **P0/P1은 평가함수 추가·DB 파라미터화로 확장**(rip-and-replace 금지). 선언적 룰엔진(rule-engine/ZEN)은 룰 수·복잡도가 임계 초과 시 P2 재평가.

## 7. 법적 경계 (전 항목 전제)
- 방어가능 = 행정적 청구오류 점검(필수기재·산정착오·작성요령·증빙·산정횟수·DUR 결정론 경고). 임상판단 아님.
- 의료법 27/19/21조 → **의료진 확인 게이트(#315) 유지**. 개인정보보호법 최소수집·민감정보 → 사전심사 최소필요정보. ML 2차활용은 복지부 보건의료데이터 가이드라인 가명처리.

## 8. 상세 설계 — P0 (결정론 룰)
### P0-1. 약제 DUR (G3) — 룰타입 `DRUG_CONTRA`·`DRUG_DUP_THERAPY`
- **선결**: §4-2 성분매핑. **데이터**: `claims.dur_rules`(ingredient_a·ingredient_b·type[CONTRA_COMBO|AGE|PREGNANCY|DUP]·min_age·max_age·notice_no·source·severity·active). 공개데이터 적재 잡(`job_dur_sync`, 출처표시).
- **평가함수**:
  - 병용금기: 동일 진료일(또는 처방 window) 라인의 성분쌍 ∈ dur_rules(CONTRA_COMBO) → BLOCK, detail=금기사유·공고번호.
  - 연령/임부 금기: 라인 성분 ∈ (AGE: patient_age 범위 / PREGNANCY) → BLOCK.
  - 동일성분 중복: 동일 진료일 동일 ingredient 2+ (예외 사유 코드 없을 때) → WARN.
- **수용기준**: 공개데이터 N쌍 적재, 합성 케이스에서 금기 BLOCK·정상 무적발. 멱등. 출처표시. 의료진 게이트 적용(금기는 임상맥락 有 → physician_gated 권장).
- **마이그**: 0110(dur_rules) + DrugCodeMap/ingredient 매핑(0111).

### P0-2. 거부사유 코드화 (G8) — finding 확장
- **모델**: `pre_review_findings.adjustment_reason_code`(VARCHAR), `reason_group`(A/F/K/L 등). Finding dataclass·평가함수가 룰별 reason_code 부여(매핑표 §4-3).
- **활용**: UI 적발에 사유코드 배지, 통계(`/claims/stats`)를 사유코드 분포로 확장, 학습사례 features에 포함(설명가능성·재학습).
- **수용기준**: 전 룰이 reason_code 부여, 통계 사유별 집계, 기존 detail 텍스트 병행. 마이그 0112.

### P0-3. 산정횟수 정밀화 (MUE식) — `COUNT_LIMIT` 확장
- params에 `mai`(1 라인단위 / 2 일자절대 / 3 임상벤치마크) 추가. HIRA 「최대산정횟수 점검기준」 매핑. 일자절대(MAI2)는 BLOCK, 벤치마크(MAI3)는 WARN.
- **수용기준**: params만으로 동작(엔진 함수 최소수정), 기존 케이스 회귀 없음.

## 9. 상세 설계 — P1 (분류체계·룰엔진)
- **G2 진단-수가 연계**(형식 점검만): `claims.dx_procedure_rules`(code·require_dx_prefix[]·forbid_sex·age범위). 평가 `DIAGNOSIS_MISMATCH`: 수가에 필수상병 prefix 미존재/성·연령 모순 → WARN(임상 타당성은 의료진 게이트). diagnosis_codes 활용.
- **G5 성연령 자동화**: `fee_master`에 sex/min_age/max_age 컬럼 → `AGE_SEX_LIMIT` 자동 룰생성·활성(현 템플릿 활성화). 데이터 이미 보유.
- **G1 묶음/G4 재료**: `claims.bundling_rules`(parent_code·child_codes[]·type[BUNDLE|MATERIAL])→`BUNDLE_VIOLATION`·`MATERIAL_UNBUNDLING`.
- **G7 고시 버저닝**: `pre_review_rules`에 `valid_from·valid_to` → 진료일 시점 유효룰만 평가, 효력일 자동적용·소급 재평가(재-sync). bundling/dx/dur 룰도 동일 시점관리.
- **근거 코퍼스**: HIRA 공개심의사례·부당사례 모음집 인덱싱 → 근거검색(#319) 강화.

## 10. 상세 설계 — P2 (AI, 휴먼인더루프 필수)
- **G10 삭감예측**: 학습사례(#316/#317) JSONL → XGBoost 지도학습, 제출 전 삭감확률 스코어로 워크리스트 **triage 우선순위화**(자동거부 금지·근거 제시).
- **G9 이상탐지**: Isolation Forest/autoencoder 비지도(의료진·부서·환자·시계열) → 인간검토·근거 필수.
- **G6 종별·G11 자격**: 월 자격동기화·종별 변경 재검증을 sync 파이프라인 통합.

## 11. 데이터모델·룰타입 확장 요약
- 신규 테이블: `dur_rules`·`drug_ingredient_map`·`dx_procedure_rules`·`bundling_rules`.
- 기존 확장: `pre_review_findings`(adjustment_reason_code·reason_group)·`pre_review_rules`(valid_from/to)·`fee_master`(sex/min_age/max_age)·`DrugCodeMap`(또는 별도 성분맵).
- 신규 룰타입: DRUG_CONTRA·DRUG_DUP_THERAPY·DIAGNOSIS_MISMATCH·BUNDLE_VIOLATION·MATERIAL_UNBUNDLING(평가함수 등록).
- 마이그 번호: **0110~**(현 prod 0109).

## 12. 의존성·시퀀싱
```
선결: line dict 강화(§4-1) ─┬─> P0-1 DUR (성분맵 §4-2 선결)
                          ├─> P0-3 산정횟수(독립)
거부사유 정의(§4-3) ──────> P0-2 코드화 ─> (통계·학습 확장)
P0 완료 ─> P1(진단연계·성연령·묶음·버저닝) ─> P2(AI: 학습사례 누적 의존)
```
- **착수 순서 권장**: ① 선결조건(line 강화·거부사유 정의) → ② P0-2(저위험·즉효) + P0-3 → ③ 성분맵→P0-1 DUR → ④ P1.

## 13. 리스크·완화
- DUR 오탐(과BLOCK) → 초기 WARN 운영 후 BLOCK 승격·physician_gated·킬스위치(config). 
- 공개데이터 라이선스(유형2 비상업) → 상용 활용 전 식약처/심평원 확인.
- 룰 폭증 → 시점관리(valid_from/to)·통계 기반 오탐률 모니터(기존 PR-B) 연계.
- 모든 신규 룰: P1 자동 이상없음/P2 자동 오탐 캘리브레이션 호환(auto_decided 피드백 차단 유지).

## 14. 성공지표
삭감 적중률/오탐률 추세·자동화율·DUR 경고 적시성·사전 차단 삭감액(원)·리뷰어 처리시간·학습사례 APPROVED 누적·사유코드 분포.

## 15. 비고
- HIRA 수치(사전점검 항목·부당사례 분류·DUR 통계)는 버전 의존 — 고객 문서 인용 시 현행 고시 대조.
- 본 v0.2는 코드 검증(엔진 구조·BillingLine 필드·DrugCodeMap 성분 부재) 반영. 미확인 1건: 엔진 입력 line dict의 patient_age/sex/diagnosis_codes 전달 여부(§4-1에서 착수 시 확정).

**출처**: law.go.kr·repository.hira.or.kr(심사지침 2024.7)·hira.or.kr·data.go.kr/15089525(병용금기)·cms.gov/ncci·x12.org(CARC/RARC)·github(rule-engine·zen·openimis)·mohw.go.kr·pipc.go.kr.
