# 사전심사 강화 룰 데이터 적재 런북 (P0~P1)

> P0~P1 룰 엔진은 전부 LIVE(마이그 0110~0115)이나 **룰 데이터가 비어 있으면 무영향(안전)**.
> 본 문서는 실데이터 적재 절차 + TEST 샘플 시드 시연·제거를 정리. 적재 EP는 모두 `claims.review write`(관리자).

## 0. 시연 결과 (TEST 샘플, 2026-06-23 prod 검증)
`/tmp/seed_prereview_rules.py`로 TEST 룰·청구 적재 후 sync → 전 룰 작동 확인:
| 차트 | 적발(rule_code · reason · severity) |
|---|---|
| TEST-PR1 | DUR-CONTRA(B-DUR·BLOCK)·DUR-DUP(D-DRUG·WARN)·DUR-AGE(B-DUR·BLOCK) |
| TEST-PR2 | PR-AGESEX-AUTO(B-AGESEX·WARN) |
| TEST-PR3 | DX-MISMATCH(M-DX·WARN) |
| TEST-PR4 | BUNDLE-BUNDLE(B-BUNDLE·WARN) |

## 1. 약제 DUR (P0-1)
**선결**: 보험코드↔주성분 매핑. **출처**: 식약처 DUR 품목정보 OpenAPI(data.go.kr/15059486)·병용금기 약물쌍(15089525, 라이선스 유형2 출처표시·비상업).
```
# 성분 매핑(보험코드→주성분)
POST /api/v1/claims/pre-review/dur/ingredient-map
[{"insurance_code":"643301230","ingredient_code":"A11AA","ingredient_name":"..."}]

# DUR 룰(고시 갱신 시 replace_source로 출처 전체 교체)
POST /api/v1/claims/pre-review/dur/rules?replace_source=MFDS
[{"dur_type":"CONTRA","ingredient_code":"A11","ingredient_code_b":"B22","reason":"...","notice_no":"2026-1","severity":"BLOCK"},
 {"dur_type":"AGE","ingredient_code":"C33","max_age":12,"reason":"소아금기","severity":"BLOCK"},
 {"dur_type":"DUP","reason":"동일성분 중복","severity":"WARN"}]
GET /api/v1/claims/pre-review/dur/rules
```
임부금기(PREGNANCY)는 임신여부 미수신으로 보류.

## 2. 성·연령 적합성 (P1 G5)
fee_master.sex/min_age/max_age에 성·연령 한정 수가 제약을 적재(약가·수가 마스터 동기화 시). 룰 `PR-AGESEX-AUTO`(시드됨·활성)가 전 코드 자동 점검. 제약 없는 코드는 무영향.

## 3. 진단(KCD)-수가 연계 (P1 G2)
```
POST /api/v1/claims/pre-review/dx-rules
[{"code":"수가코드","require_dx_prefixes":"C,D0","name":"암수술","reason":"악성종양 상병 필요","severity":"WARN"}]
GET /api/v1/claims/pre-review/dx-rules
```
형식 점검만(필수 상병 prefix 미기재). 임상 타당성은 의료진 게이트(의료법).

## 4. 묶음청구 (P1 G1/G4)
```
POST /api/v1/claims/pre-review/bundling-rules
[{"parent_code":"패키지수가","child_codes":"세부1,세부2","rule_type":"BUNDLE","reason":"별도산정 불가","severity":"WARN"},
 {"parent_code":"수술수가","child_codes":"재료1","rule_type":"MATERIAL","reason":"수술 포함 재료","severity":"WARN"}]
GET /api/v1/claims/pre-review/bundling-rules
```

## 5. 적재 후 재평가
룰/데이터 적재 후 기존 청구는 **재-sync 시 시점 기준 재평가**(고시 버저닝 valid_from/to 반영). 야간 `job_prereview_sync`(당월·전월) 또는 표적 sync로 반영. 신규 청구는 실시간 파이프라인이 자동 적용.

## 6. 안전·거버넌스
- 룰 데이터 빈 상태 = 적발 0(무영향). 초기 severity는 WARN 운영 후 통계(거부사유 분포·오탐률) 확인하고 BLOCK 승격.
- 모든 적발은 거부사유 코드(A/B/D/K/M) 부여 → 통계·학습 피처. 자동 이상없음(P1)·자동 오탐(P2 캘리브레이션) 호환.
- DUR 공개데이터 라이선스(유형2 비상업) 준수 — 상용 활용 전 식약처/심평원 확인.

## 7. TEST 샘플 시드 제거
```sql
DELETE FROM claims.dur_rules WHERE source='TEST';
DELETE FROM claims.drug_ingredient_map WHERE insurance_code LIKE 'TEST-%';
DELETE FROM claims.dx_procedure_rules WHERE code LIKE 'TEST-%';
DELETE FROM claims.bundling_rules WHERE parent_code LIKE 'TEST-%';
DELETE FROM claims.pre_review_findings WHERE case_id IN
  (SELECT id FROM claims.pre_review_cases WHERE chart_no LIKE 'TEST-PR%');
DELETE FROM claims.pre_review_cases WHERE chart_no LIKE 'TEST-PR%';
DELETE FROM patient.billing_lines WHERE line_id LIKE 'TEST-PRL-%';
DELETE FROM regulatory.fee_master WHERE code LIKE 'TEST-%' AND code IN
  ('TEST-DRUGA','TEST-DRGA2','TEST-DRUGB','TEST-GYN','TEST-OPC','TEST-PKG','TEST-CHILD');
```
(성연령 자동 룰 `PR-AGESEX-AUTO`는 시드 아닌 정식 룰이므로 유지.)
