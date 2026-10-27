# hospital-erp Gap Analysis (Check #1)

> **Date**: 2026-06-07 · **Match Rate**: **87.2%** (123/141 가중점)
> **대상**: 백엔드 Phase 1 (services/core, 커밋 accb76b) vs Design v0.3
> **판정**: 70~90% 구간 — Act 1회 반복 권장 (골격 보강으로 90%+ 도달 가능)

## 카테고리별

| 카테고리 | 점수 | 상태 |
|----------|:----:|:----:|
| 데이터 모델 (§3) | 96% | ✅ EXCLUDE·REVOKE·CHECK 제약 1:1 |
| 보안 설계 (§6) | 97% | ✅ GCM 포맷·CAS 회전·blind index 등 완전 준수 |
| 비즈니스 규칙 | 98% | ✅ temporal·FIFO·해시체인·연차·기한·역분개 전부 |
| API (§4.3) | 83% | ⚠️ 조회 엔드포인트 일부 누락 |
| 모듈 인터페이스 (§4.4) | 79% | ⚠️ interfaces.py 레이어 미분리 (동작은 정상) |
| 연계 (§5) | 64% | ❌ ACL Protocol 4종 미선언, reprocess API 부재 |
| 워커 (§11.0) | 65% | ❌ 근태·백업·파기 잡 미등록 |

## 실 Gap (Act-1 대상)

1. [High] ACL Protocol 4종 선언 누락 (HIS·그룹웨어·Hira·FeeMaster — AIIndexPublisher만 존재)
2. [High] `POST /admin/staging/{id}/reprocess` 미구현 (E2E ⑥ 경로)
3. [Med] `GET /notices`·`GET /notices/{id}/dispatch-status` 누락
4. [Med] 공통 `export.xlsx` 엔드포인트 + access_log(EXPORT) 골격 누락
5. [Med] 근태 집계 일배치(`job_attendance_summary`) 미등록 (+백업·파기 잡)
6. [Low] 조직 관리 정책 키 재사용 (`regulatory.notices` → `common.departments` 신설)
7. [Low] 듀티 라우터 violations[] 응답 — 확인 결과 구현됨 (라우터 반환에 포함)

## 역방향 Gap (구현이 정당 — 설계 동기화)

부서 CRUD, movements receive/issue 분리, `GET /leaves/remaining`, `GET /budgets/execution`, `fee_sync_runs` 테이블, VoucherService(voucher_on·created_by), claim_batches(decided_on·notice_arrived_on·staging_id)

## 범위 외 (의도된 미구현 — Gap 미산입)

프론트(apps/web 28화면), Phase 2 FR(41~45·50), Gate 상세(G1 파서·G2 산식·G3 확정값·G5 영업일), 어댑터 구현체(M0a/b), Excel/PDF 렌더링 엔진, Phase 3 QA

## Act-1 계획

Gap 1~6 보강 + 설계 문서 동기화 → 예상 Match Rate 93~94%

---

# Check #2 — Act-1 재검증 (2026-06-07)

> **Match Rate: 94.3%** (138/141) — ✅ 90% 게이트 통과 (커밋 5814967 반영)

| Gap | 결과 |
|-----|------|
| 1 ACL Protocol 4종 | ✅ 해소 (§5.1 시그니처 일치) |
| 2 reprocess API | ✅ 해소 (FAILED 가드·audit) |
| 3 공시 조회·발신상태 | ✅ 해소 |
| 4 export 골격 | 🔶 부분 (scope 합성·EXPORT 접속기록 달성 / openpyxl 렌더링·모듈별 경로는 후행) |
| 5 배치 잡 3종 | ✅ 해소 (전부 진료시간 외) |
| 6 정책 키 | ✅ 해소 (common.departments) |

카테고리 변동: 연계 64→94%, 워커 65→96%, API 83→95%. 잔여는 전부 Low/의도된 후행(M0·운영 의존).
**판정: Act 반복 종료 — report 단계 진입 가능.**

---

# Check #3 — 프론트 포함 전체 (2026-06-07)

> **Match Rate: 82.3%** (백엔드 94.3% ×0.65 + 프론트 60.0% ×0.35) — 프론트 화면 커버리지 57.8%가 견인

## Act-2 (커밋 4743de9) — High 1~4 + Medium 5·6·8·11·12 해소

| Gap | 조치 |
|-----|------|
| 삭감 드릴다운 | 백엔드 GET /claims/statements 신설 + 배치→명세→조정 UI |
| 예약 캘린더 | GET /reservations 신설 + 일자별 화면 (zod) |
| 급여명세/입력 | /hr/payroll — 본인 명세(실수령액)+관리자 입력·확정 |
| zod 미통합 | login·환자·예약 zodResolver |
| 수가 조회/발주 보드/입고/근태/증명서 | 화면 5종 신설 |
| AI 검토 큐 승인 | POST /dispatch/{id}/approve (hash 바인딩·audit) + 승인 버튼 |

화면 커버리지 재산정: 완전 23 + 부분 5(×0.5) = 25.5/32 = **79.7%**, 스택(zod 통합) 65→80%
→ 프론트 영역 = 79.7×0.7 + 80×0.3 = **79.8%**
→ **통합 = 94.3×0.65 + 79.8×0.35 = 89.2% → Act-2 후속 미세 보강 포함 시 90% 경계**

잔여 (Low/의도 후행): 직원 상세 탭·듀티 본인 조회·by=doctor 토글·features/ 구조·shared-types — v0.5 설계 주석으로 후행 명시 또는 W3

---

# Act-3 (마이크로) + 최종 판정 (2026-06-07)

커밋 3741979 — 직원 상세(발령/면허 탭·scope 검증·접속기록), 듀티 본인 조회(GET /hr/duties/me), 삭감 통계 의사별 토글.

화면 커버리지 재산정: 완전 26 + 부분 3(×0.5) = 27.5/32 = **85.9%**
프론트 영역 = 85.9×0.7 + 80×0.3 = **84.1%**
**통합 Match Rate = 94.3×0.65 + 84.1×0.35 = 90.7% ✅ 90% 게이트 통과**

잔여 (의도된 후행 — 설계·코드 주석 명시): 대시보드 위젯 확장, 휴가 신청 목록, features/ 구조·shared-types 리팩터(import-linter 도입 시), shadcn 전환, Gate G1~G5 의존 기능.
**판정: Check 종료 — report 단계 진입.**

---

# 통합 검증 (Zero Script QA 사전 점검) — 2026-06-07

환경: compose PG15(:55432)+Redis, alembic 4개 마이그레이션, 합성 시드, core(uvicorn)

## 시나리오 12/12 ✅

| # | 시나리오 | 결과 |
|---|---------|:---:|
| 1-2 | 로그인 성공 / 오류 ERP-1001 | ✅ |
| 3 | STAFF→재무 RBAC 차단 (ERP-1004) | ✅ |
| 4-5 | **temporal 경계** — 6/30=15,000 / 7/1=15,300 (시행일 당일 신버전) | ✅ |
| 6 | **FR-53 우회 차단** — 전각+체크섬 유효 주민번호 → BLOCKED(RRN 마스킹) / 체크섬 무효는 오탐 억제 | ✅ |
| 7-9 | 환자 검색→접수→수납→**일마감→전표(F1)**→마감 후 수납 차단 | ✅ |
| 10-11 | 마약류 입고(seq=1)→정정(balance 시스템 산출 45) | ✅ |
| 12 | 접속기록 적재 | ✅ |
| + | **EXCLUDE 기간 겹침** — PG에서 INSERT 거부 실증 (`ex_fee_overlap`) | ✅ |

## 검증이 잡은 실 버그 2건 (수정 완료)

1. **alembic advisory lock 트랜잭션 분리 버그** — lock용 exec_driver_sql이 암묵 트랜잭션을 열어 마이그레이션 전체가 조용히 ROLLBACK (exit 0). → lock을 AUTOCOMMIT 별도 세션으로 분리
2. **시드 이메일 `.test` TLD** — email-validator가 special-use 도메인 거부 → `@demo-hospital.example`

기타: 로컬 PostgreSQL(5432) 충돌 → compose 55432 리매핑.

---

# 코드 검토 (code-analyzer + frontend-architect) — 2026-06-07

## 발견·수정 26건 (커밋 cd5f3ea, 테스트 139)

**백엔드 (code-analyzer — High 5·Med 6·Low 5)**
- 🔴 권한 결함 3건: patient write 정책 부재(read로 환자 생성·수납 가능), export items·list_items 검증 누락 → POLICY read/write 정비 + require_scope 단일 게이트 + 회귀 테스트 7건
- 🔴 워커 2건: 트랜잭션 내 30s 백오프(커넥션 점유), 상주 루프 busy-loop → 구조 수정
- 🟡 N+1 5개소 단일 집계/IN-batch, 전표 균형 Decimal, 정책 차용 제거, 접속기록 2개소
- ✅ 확인된 클린: UoW 규약·모듈 경계(전수 grep 0건)·에러 체계

**프론트 (frontend-architect — 실버그 3·심각 4·경고 7)**
- 🔴 Excel <a href> 무인증 401, 업로드 api.ts 우회(토큰 갱신 불가), refresh 이중 회전 → apiDownload/apiUpload/2s 윈도
- 빈 ID·중복 클릭 가드, 4페이지 silent fail 해소, 듀티 930셀 memo, 사이드바 매칭, 모달 ESC

**잔여 (의도 후행 기록)**: 메시지 배너 컴포넌트화·src/types 중앙화·Button isLoading·포커스 트랩 — 화면 30+ 시점 리팩터와 함께

---

# Check #4 — Gate 구현 누적분 재검증 (2026-06-07)

## 판정: 백엔드 신규 영역 95.8% / 통합 **~90.6%** — 90% 게이트 유지 ✅

| 영역 | Match | 비고 |
|------|:---:|------|
| G2 간호등급 | 98% | references 원문과 1:1 정합 — 설계 "보류"를 초과 해소 |
| G5 기한 산정 | 97% | 법령 4규칙 정합 |
| 렌더링 §6.7 | 99% | url_fetcher·autoescape 완전 준수 |
| MEDRLT 파서 | 93% | §5.2 파이프라인·멱등·격리 정합. G1 한정자 = 의도된 운영 게이트 |
| 권한 | 88%→**해소** | claims·hr 로컬 헬퍼 → require_scope 수렴 (즉시 수정) |

## 즉시 수정 반영 (Act-4 경량, 같은 날)
1. `AppealService.open` — 처분일(batch.decided_on) 조회 → **180일 상한 전달** (법 §87 완전 구현)
2. claims·hr 라우터 단일 권한 게이트 수렴 — 5/5 모듈 통일 (M5 완결)

테스트 171 유지. 잔여 gap = G1 한정자(실물 게이트)·HIS 데이터 소스 — 전부 외부 의존으로 분류.
