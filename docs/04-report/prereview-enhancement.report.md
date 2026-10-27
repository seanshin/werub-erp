# 사전심사(Claims Pre-Review) 강화 — 완료 보고서

> **기간**: 2026-06-23 (단일 세션) · **PR**: #413~#422 · **마이그**: 0110~0115
> **결과**: 정형 룰 10종 → **17+종**(HIRA 자동점검·NCCI 분류 정합) + 거부사유 코드체계·고시 버저닝·삭감위험 triage
> **연관**: 기획서 [prereview-enhancement.plan.md](../01-plan/features/prereview-enhancement.plan.md) ·
> 데이터 적재 런북 [prereview-rules-data-load.md](../05-reference/prereview-rules-data-load.md) · [[erp-claims-auto-review]]

## 1. 요약
화면 검토 요청에서 출발해 (1) 현행 전수 매핑 → (2) 빠진/약한 영역 12종 식별 + HIRA·국제표준·오픈소스 조사(기획서) → (3) 코드검증 상세설계(v0.2) → (4) **P0~P2-a 전부 구현·배포**. 모든 신규 룰은 **결정론·거부사유 코드 연계·고시 시점관리·의료법 게이트 호환**, 룰 데이터 빈 상태에선 무영향(안전). **엔진·적재EP·화면은 prod LIVE이나 운영 룰 데이터는 아직 미적재** — 실제 청구에는 데이터 적재(§5 런북) 전까지 적중하지 않는다. §4 시연은 수동 주입한 TEST 샘플 행으로 각 룰의 발화를 확인한 것(운영 적중 아님).

## 2. 구현 매트릭스
| 단계 | 항목 | 핵심 | PR | 마이그 |
|---|---|---|---|---|
| P0-2 | 거부사유 코드화 | finding reason_group/code(A·B·D·K·M)·stats by_reason_group | #413 | 0110 |
| P0-3 | 산정횟수 MUE식 | COUNT_LIMIT params.mai(라인/일자, MAI1·2·3) | #414 | — |
| P0-1 | 약제 DUR | dur_rules·drug_ingredient_map·evaluate_dur(병용금기·동일성분중복·연령금기) | #415 | 0111 |
| P1 G5 | 성·연령 자동화 | fee_master sex/min_age/max_age·AGE_SEX 자동모드(PR-AGESEX-AUTO) | #417 | 0112 |
| P1 G7 | 고시 버저닝 | pre_review_rules valid_from/to·진료일 시점 유효룰·소급 재평가 | #418 | 0113 |
| P1 G2 | 진단(KCD)-수가 연계 | dx_procedure_rules·evaluate_dx(필수상병 prefix 형식점검·M-DX) | #419 | 0114 |
| P1 G1/G4 | 묶음청구·재료 별도산정 | bundling_rules·evaluate_bundle(BUNDLE/MATERIAL 언번들) | #420 | 0115 |
| (fix) | ruff E741 복구 | #415 lint 실패 핫픽스 | #416 | — |
| P2-a | 삭감위험 triage | 확정 인정률 기반 예측삭감·우선순위(휴먼인더루프) | #422 | — |
| 문서 | 기획서·v0.2·데이터 런북 | 갭분석·조사·상세설계·적재절차 | #411·#412·#421 | — |

## 3. 엔진·아키텍처
- 순수 평가함수 + DB 주입(룰 테이블 JSONB) 패턴 유지·확장(rip-and-replace 없음). 신규 엔진 메서드: `evaluate_dur`·`evaluate_dx`·`evaluate_bundle`(데이터 주입형 순수함수), 기존 `_age_sex_limit` 자동모드 확장, `_count_limit` MAI.
- 거부사유 코드: `REASON_BY_TYPE`(룰타입→A/B/D/K/M) 스탬프 → 설명가능성·통계·학습 피처.
- 적재 EP(관리자): `/claims/pre-review/dur/{ingredient-map,rules}`·`/dx-rules`·`/bundling-rules`(bulk·고시 갱신 교체).
- 모든 신규 룰 worklist sync 자동 파이프라인 통합(실시간·야간 백필).

## 4. 검증 (수동 주입 TEST 샘플 발화 시연, 2026-06-23 — 운영 룰테이블 미적재)
| 차트 | 적발(rule · reason · severity) |
|---|---|
| TEST-PR1 | DUR-CONTRA(B-DUR·BLOCK)·DUR-DUP(D-DRUG·WARN)·DUR-AGE(B-DUR·BLOCK) |
| TEST-PR2 | PR-AGESEX-AUTO(B-AGESEX·WARN) |
| TEST-PR3 | DX-MISMATCH(M-DX·WARN) |
| TEST-PR4 | BUNDLE-BUNDLE(B-BUNDLE·WARN) |
- 항목별 단위테스트(ruff+pytest) 통과. 격리 컨테이너 검증 → prod 배포(alembic 0115)·헬스 200.
- ⚠️ 위 TEST-* 행은 시연용으로 **수동 주입한 샘플**이며 운영 룰테이블(dur_rules·dx_procedure_rules·bundling_rules·institution_surcharge_rates)은 시드 0행 — 실 청구 적중은 §5 데이터 적재 후.

## 5. 데이터 활성화 (운영 단계)
룰 엔진은 LIVE이나 **룰 데이터 적재 시 작동**. 출처·절차는 런북 참조:
- DUR: 식약처/공공데이터포털(병용금기 약물쌍·DUR 품목) — 라이선스 유형2(출처표시·비상업) + 보험코드↔주성분 매핑.
- 성연령: fee_master 성·연령 한정 수가 제약(마스터 동기화).
- 진단연계·묶음: HIRA 상병-수가·패키지 기준 적재.
- 적재 후 재-sync로 기존 청구 시점 기준 재평가. 초기 WARN → 통계(거부사유·오탐률) 확인 후 BLOCK 승격.

## 6. 법적 경계 (준수)
- 행정적 청구오류·산정기준 점검에 한정. 임상 의학판단은 **의료진 확인 게이트**(의료법 제27조, #315) 유지 — 진단연계는 "필수상병 미기재" 형식 점검만.
- 개인정보 최소수집(사전심사 최소필요정보)·DUR 응답은 집계만(memo 원문 미반환).

## 7. 잔여 (데이터/전문가 의존)
- **P2-b 본격 ML**(XGBoost 삭감예측)·**G9 이상탐지**(IsolationForest) — 확정 학습사례 충분 누적 후. 현 triage(P2-a)가 경량 대체.
- DUR 임부금기(PREGNANCY) — 임신여부 미수신.
- 자격 검증 심화(G11)·종별 재검증(G6) — sync 파이프라인 후속.

## 8. 비고
- CI 교훈: #415가 ruff E741로 lint 실패했으나 브랜치 보호 미강제로 머지 → #416 핫픽스. 이후 머지 전 CI 그린 확인·컨테이너 ruff 검증 포함.
- HIRA 수치(사전점검 항목·부당사례 분류·DUR 통계)는 버전 의존 — 고객 문서 인용 시 현행 고시 대조.
