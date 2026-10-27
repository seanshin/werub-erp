# ADR — 회계·재무 분석 도구 스택 결정 (D-7)

> **결정일**: 2026-06-23 | **결정자**: 운영자(사용자) | **상태**: ✅ 확정
> **근거 조사**: deep-research(소스 22개 → 주장 83개 → 25개 적대적 검증, 22개 확정, 2026-06-23)
> **제약**: 병원 온프레미스 **격리망**(인터넷 단절·외부 SaaS 불가) · 라이선스 **MIT/permissive(Apache-2.0·BSD) 강선호**, AGPL/상용/카피레프트=도입 부담
> **현황**: K-IFRS ~95% 성숙(복식부기 전표 기반·전체 감사추적). 분석은 `erp.analytics` 집계 BI 단계 → 다음 단계는 *검증·이상탐지·예측*

## 결정 (카테고리별 1순위)

| 카테고리 | 1순위 | 라이선스 | 근거 |
|---|---|---|---|
| **회계 감사·이상탐지** (착수점) | 자체 구현(`erp.analytics.audit`) + `benford_py`·scikit-learn `IsolationForest` | BSD-3 | 성숙 전표 데이터 활용도 최고·신규 인프라 0 |
| **통계적 예측(시계열)** | `statsforecast`(Nixtla) / `statsmodels` | Apache-2.0 / BSD-3 | permissive·Stan 네트워크 의존 없음 |
| **셀프호스트 BI** | **Apache Superset** | Apache-2.0 | core에 EE 칸막이 없음·PostgreSQL 네이티브 |
| **프론트 데이터 그리드** | **TanStack Table** | MIT | 기존 TanStack Query와 동일 생태계 *(LICENSE 재확인 필요)* |

## 카테고리별 라이선스 검증 결과

### 1. 셀프호스트 BI
- **Apache Superset — Apache-2.0** ✅ (1순위). Lightdash(MIT core, 단 EE 디렉토리 별도 라이선스 open-core), Evidence.dev(MIT, 개발자향 code-as-BI).
- ❌ **Metabase 배제** — 무료판은 **AGPL-3.0**(네트워크 카피레프트 §13), **격리망(air-gap) 기능은 유료 Enterprise 전용**(무료판은 `token-check.metabase.com` 아웃바운드 호출 존재). 검증에서 "무료판으로 격리망 가능" 주장 **반박(1-2)**. 귀사 기준(AGPL/상용=부담)에 정확히 저촉.

### 2. 회계 감사·이상탐지
- `benford_py` — **BSD-3** ✅ (Benford 법칙, 부정·오류 탐지 목적)
- scikit-learn `IsolationForest` — **BSD-3** ✅ (비정상 분개·금액 이상치)
- PyOD — BSD-2 ✅(IForest 포함), 단 ⚠️ "순수 파이썬 오프라인 설치" 주장 **반박(0-3)** — 컴파일 의존성, wheel 미러링 사전 검증 필요

### 3. 통계적 예측
- `statsforecast`(Nixtla) — **Apache-2.0** ✅ / `statsmodels` — **BSD-3** ✅ / `sktime` — **BSD-3** ✅
- Prophet — **MIT** ✅ (라이선스는 OK) — ⚠️ 그러나 **Stan 백엔드(CmdStanPy)가 GitHub에서 CmdStan 다운로드** → 격리망 설치 마찰(`CMDSTAN` 환경변수 수동 우회 필요). 따라서 설치 깔끔함에서 statsforecast/statsmodels 우위.
- 공통 토대 `pandas`·`numpy` — BSD ✅

### 4. 프론트 데이터 그리드 — ⚠️ 본 조사 검증 미포함(재확인 필요)
- **TanStack Table — MIT** (1순위, 스택 일관성), Glide Data Grid — MIT, **ag-grid — Community판 MIT / Enterprise판 상용**(피벗·그룹핑 등 고급기능 유료 ⚠️)
- 도입 시 각 LICENSE 직접 확인 필요(이번 검증 세트에 표 누락).

## 격리망 도입 함정 (체크리스트)

1. **Metabase 무료판 ≠ 격리망** — air-gap은 유료. 도입하지 않음.
2. **Prophet** — MIT지만 Stan 다운로드. 채택 시 CmdStan 오프라인 사전 반입.
3. **ag-grid** 고급기능 = 상용. Community(MIT) 범위만 사용하거나 TanStack Table 사용.
4. **PyOD/scikit-learn/Prophet** — 오프라인 wheel 세트 end-to-end 설치 테스트 필요.

## 구현 방침 (단계)

- **v1 (즉시·신규 의존성 0)**: `erp.analytics.audit` — 순수 SQL/Python. Benford 법칙(자체 구현, 디지트 분포+χ²+MAD), 중복전표/중복지급 탐지, 비정상 분개(주말·심야·라운드넘버·승인한도 직전 분할). 격리망 무관. POSTED 전표만 대상·자동판정 아님(리뷰 후보 제시).

  **설계 보완(2026-06-23 검토 반영):**
  1. **자동전표 오탐 차단** — 비정상 분개는 `source_module="manual"`(사람 입력)만 검사. 야간 감가상각·일마감·청구sync 배치는 주말·심야 전기가 정상이므로 제외.
  2. **타이밍 기준 = `created_at`(입력시각)** — `voucher_on`(회계일, 월말이 주말일 수 있음)이 아닌 실제 입력 시각. KST 변환·레거시(created_at 없음) 시 타이밍 검사 생략.
  3. **중복은 적요(거래처) 필수**(`require_detail=True`) — 적요 없는 동일금액(본인부담 1만원 등)은 정상분개 오탐 과다 → 제외(거래처 신호 있을 때만 중복 후보).
  4. **잔여 캐비엇**: Benford는 자릿수 분산 필요(금액 군집 시 거짓 부적합)·표본<300 신뢰도 낮음 표시 / 중복 window는 보수적(최초~최종 간격) / 라운드 검사는 전표 최대 라인 기준 / 범위 전체 Python 그룹화(연 단위 스코프로 허용, 대량 시 SQL HAVING 전환 고려).
- **v2 (선택)**: scikit-learn `IsolationForest`(BSD)로 계정별 금액 이상치 교차검증 — 오프라인 wheel 반입 후.
- **v3 (선택)**: `statsforecast`/`statsmodels`로 현금흐름·매출 통계 예측(현 룰베이스 시뮬레이션 격상).
- **BI(Superset)**: 비개발자 셀프서비스 수요 확인 시 별도 컨테이너 + PostgreSQL **읽기 전용 복제본/계정** 연결.

## 유효기간·후속

- 오픈소스 라이선스는 릴리스 간 변경될 수 있음(특히 open-core BI). **도입 시점 재검증** 필수.
- 미결: 프론트 그리드 라이선스 직접 확인, PyOD/Prophet 오프라인 설치 검증, Superset 배포 버전 플러그인 라이선스.
