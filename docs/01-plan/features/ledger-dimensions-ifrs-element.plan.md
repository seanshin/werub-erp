# 기획: 원장 다차원 + 계정 IFRS element (G8·G9) + XBRL 스코핑 노트 (G10)

> 근거: `docs/06-compliance/ifrs/ledger-architecture-controls.md` 갭 G8·G9·G10.
> 성격: 대규모 기능 — 단계별·회귀 봉쇄(골든 테스트) 필수. 작성 2026-07-14.
> 의존 순서: **G8(계정 element) → G9(라인 차원) → G10(XBRL)**. G8은 G9 리포팅·G10 태깅의 기반.

---

## 1. 배경

현행 재무제표 분류가 `finance/statements.py`에 **하드코딩**(계정코드 prefix/set)돼 있다(G8):
- 유동/비유동: `_CURRENT_ASSET_PREFIX="11"`, `_CURRENT_LIAB_PREFIXES=("21","23")` − `_NONCURRENT_LIAB_CODES={2120,2130,2510}` (`statements.py:24-26`).
- 현금흐름 활동: `_cf_activity()` OP/INV/FIN 계정코드 세트 (`statements.py:19-45`).
- 기능별(P&L): 이미 `accounts.cost_function`(COGS/SGA/OTHER) 컬럼화됨.

라인 차원은 `voucher_lines.department_id` **단일**뿐 — cost_center/project/segment/통화 없음(G9). IFRS 8 부문·관리회계 정밀도·외화 라인 정밀이 제약. XBRL(G10)은 계정↔IFRS element 매핑이 전제라 G8 없이는 불가.

## 2. G8 — 계정 IFRS element (데이터 주도 분류)

**목표**: 하드코딩 분류를 계정마스터 컬럼으로 이관 → 데이터 주도·추적 가능·XBRL 기반.

### Phase 0 (무위험·본 릴리즈) ✅
- `accounts.is_current`(bool null), `accounts.cf_activity`(varchar null) 추가.
- 마이그에서 기존 하드코딩 규칙으로 백필(코드=데이터 미러).
- accounts 조회/생성/수정 API·finance-accounts 화면에 노출·편집.
- **statements.py 미변경 → 재무제표 동작 불변(회귀 0).**

### Phase 1 (핵심 위험·골든 테스트 필수) ✅ v1.106.0
- `statements.py`가 컬럼을 **우선** 읽고, NULL이면 기존 하드코딩을 폴백(`_class_map`·`_is_current_acct`·`_cf_activity_of`). 적용: `financial_ratios`(유동비율)·`cash_flow_statement`(활동분류).
- **회귀 봉쇄**: 기존 재무제표/비율/현금흐름 테스트 71건이 폴백으로 전부 그대로 통과(등가성 입증) + 오버라이드 테스트(컬럼이 prefix를 이김) 추가.
- 하드코딩 세트는 폴백으로만 잔존(점진 제거 가능).

### Phase 2 (관리)
- 관리자 UI에서 element 매핑 일괄 편집·검증(미매핑 계정 경보).
- (선택) 단일 `ifrs_element` 표준 태그 컬럼 도입 → G10 XBRL 대비.

**리스크**: Phase 1이 재무제표 산식을 건드림 → 골든 테스트가 유일 안전망. cf_activity/is_current 미매핑 계정은 폴백 유지로 안전.

## 3. G9 — 라인 확장 차원 (cost_center/project/통화)

**목표**: 부문 리포팅·관리회계·외화 라인 정밀. **가치는 차원이 채워질 때 발생** → 서두르지 않음.

### Phase 0 ✅ v1.107.0
- `voucher_lines.cost_center`(varchar20)·`project`(varchar30) nullable 추가(마이그0195·무위험). 개별 컬럼 채택(리포팅 쿼리 편의). 통화 라인화(currency·amount_ccy·fx_rate)는 forex_service 통합 결정 필요 → 별도 후속.
- VoucherLineDTO 확장(기본 None·기존 호출부 무영향) → create·draft/approve·reverse 전 경로 배선(차원 보존).

### Phase 1 ✅ v1.108.0 (수기 캡처) + v1.113.0 (자동채움 1차)
- 수기 전표 라인 입력(/finance)에 코스트센터·프로젝트 선택 입력 + `VoucherLineBody`·`VoucherLineDTO(**model_dump)`로 자동 배선.
- **자동채움 1차(v1.113·감가상각)**: `AssetService._post_month`가 자산 소유부서(`FixedAsset.department_id`) 코드를 감가 전기 라인 `cost_center`로 스탬프(책임센터 관례·배치 캐시). ⇒ 감가비용이 부서별 cost center로 자동 귀속돼 dimension_summary 리포트에 반영. 나머지 사이트(환자수익=dept_code·HR·SCM)는 dept 컨텍스트 플러밍 후속.

### Phase 2 ✅ v1.108.0 (리포팅 1차)
- `StatementService.dimension_summary`(cost_center|project별 차·대·순액·POSTED+REVERSED)·`GET /finance/dimensions/summary`·/finance "차원별 집계" 카드(dim 토글). IFRS 8 정밀 부문 리포팅·외화 라인 재평가는 심화 후속.

**리스크**: 낮음(추가·점진). 단, JSONB vs 개별 컬럼은 리포팅 쿼리 편의로 결정.

## 4. G10 — XBRL/전자공시 (⚠ 규제 요건 선결)

**⚠ 엔지니어링 前 필수 확인**: 비영리 의료법인이 실제로 **DART XBRL 전자공시 대상인지**. 대개 의료기관 회계기준 보고이고 XBRL 전자공시 대상이 아닐 수 있음 — **불필요하면 스코프 제외**(만들 이유 없음). 외부 회계사/규제 확인 필요.

**필요 시에만(별도 프로젝트)**:
1. 택소노미 선정(K-IFRS DART taxonomy 버전).
2. 계정↔IFRS element 매핑(**G8 기반**·Phase 2 표준 태그).
3. iXBRL/XBRL 인스턴스 생성기 + 검증(Arelle 등).
4. 제출 워크플로.

**결론**: G10은 규제 확인이 게이트. 확인 전 착수 금지.

## 5. 권장 실행 순서

1. **G8 Phase 0**(본 릴리즈·무위험) — 컬럼+백필+관리화면.
2. G8 Phase 1 — 골든 테스트 후 statements.py 데이터 주도 전환.
3. G9 Phase 0 — 라인 차원 컬럼(무위험) → 점진 채움.
4. G10 — 규제 요건 확인 후 결정.
