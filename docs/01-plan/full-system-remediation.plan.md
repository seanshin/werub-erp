# 전체 시스템 수정·보완 계획서 (v1.78.0 기준)

- 작성일: 2026-07-13(UX 심층 조사 반영 1차 개정) · 기준 헤드: v1.78.0(#792) · 마이그 0189 · 테스트 1213
- 조사 방법: 4갈래 병렬 적대적 조사 — ①프론트엔드(apps/web 77화면 전수) ②백엔드 잔여 백로그(코드 마커·게이트·외부의존 전수) ③테스트 품질/커버리지 ④운영·보안 태세(infra·runbook·CI) + **UX/UI 3갈래 심층 감사**(디자인 시스템·IA/워크플로우·핵심 10화면 — 상세는 `docs/01-plan/features/ux-ui-improvement.plan.md`)
- 선행 맥락: v1.60.0~v1.78.0(19릴리즈)로 **백엔드 도메인 심층 재검토 백로그·설계결정 2건·배치 계층 하드닝은 전부 소진**. 본 계획서는 그 이후 남은 갭의 전수 카탈로그와 우선순위 로드맵이다.
- **하위 기획서**: UX·UI 개선은 `features/ux-ui-improvement.plan.md`(본 문서 P0-5/6/7·P0-9·P2-9/10/11·P3-8/9의 상세 SoT — UX-0 기능결함 8건·R0~R6 로드맵)

---

## 0. 요약 — 우선순위 티어

| 티어 | 성격 | 항목 수 | 핵심 |
|---|---|---|---|
| **P0** | 침묵 실패 방지·즉시 안전망 (1주) | 9 | 백업 실패 경보 · 딥 헬스체크+워커 하트비트 · 인증 레이트리밋 · prod env 확인 2건 · FE 에러화면/쿼리 에러상태 · **UX-0 기능결함 8건(접수 dept "IM" 하드코딩=데이터 오염 진행 중·수납 CARD 고정·금액 안 보이는 전표 승인 등)** |
| **P1** | 재발 방지 테스트 안전망 (2~3주) | 6 | HTTP 계층 테스트(이번 분기 escape 2건의 직접 원인) · RBAC 매트릭스 · 워커 잡 테스트 · alembic check · downgrade 왕복 · 락파일 |
| **P2** | 운영 성숙·품질 부채 (1~2개월) | 12 | 오프사이트 백업 · 롤백 절차 · 메트릭/경보 · 시크릿 파일 주입·회전 · PG 전체 테스트 레인 · FE 로딩/RBAC정합/페이지네이션 · ITAM 테스트 분할 |
| **P3** | 내부 잔여·정밀화 (분기 내) | 10+ | 간이세액표·주민번호 채널(연말정산 前) · 공휴일 캘린더 · drug_matcher 단위 · 백업 세대 계층 · FE 정리 배치 |
| **EXT** | 외부 의존 트랙 (코드 무관·조율) | 20+ | **최상위 병목 = MEDRLT 실물 샘플(E-1)** → G1 게이트 → weve 캘리브레이션 연쇄 · HIS 요청서 1건 신규(카드환불 method) |

**이미 건강한 기반**(재작업 불요): 백업 AES-256-GCM·컬럼암호 봉투(key_version+AAD)·PII 로그 마스킹·JSON 구조화 로그·prod 시크릿 fail-closed·계정 잠금·풀 튜닝·마이그 advisory lock·비루트 컨테이너·import-linter 7계약·web 락파일·인바운드 소비(SKIP LOCKED·건별 savepoint·HMAC ENFORCE)·스케줄러 리더 선출+월경계 catch-up.

---

## 1. P0 — 침묵 실패 방지 (1주 내)

### P0-1. 백업 침묵 실패 경보 [OPS·HIGH·S]
- 현상: `workers/scheduler.py` job_backup 실패가 `logger.error`만 — 수 주간 백업 0개여도 인지 불가(runbook §9에 pg_dump 버전 오류가 실제 이력).
- 조치: 실패 시 ADMIN Notification(기존 패턴 재사용) + "마지막 성공 백업 나이 >26h" 검사(스케줄러 잡 또는 딥 헬스에 포함).

### P0-2. 딥 헬스체크 + worker 하트비트 [OPS·HIGH·S]
- 현상: `/api/health`가 DB/Redis 미확인 상수 응답. worker-batch가 행업하면(컨테이너 생존) **법정 접속기록 flush(1분)**·백업·인바운드 소비·BI 마트가 전부 조용히 정지 — 감지 수단 없음.
- 조치: ① `/api/health/deep` = `SELECT 1`+Redis ping ② worker-batch가 Redis 하트비트 키 기록 → 딥 헬스가 나이 검사 ③ compose에 worker×2·web healthcheck 추가.

### P0-3. 인증 계열 레이트리밋 [SEC·HIGH·M]
- 현상: `/auth/login`은 계정 잠금(5회→15분)만 → **이메일만 알면 타 직원 계정 반복 잠금(가용성 공격)**. `/auth/pin`·`/auth/2fa/*`·`/auth/sso/his`는 서버측 스로틀 전무(PIN=저엔트로피). 기존 `common/ratelimit.py`는 인프로세스라 인증용 부적합.
- 조치: Redis 기반 IP+계정 슬라이딩윈도우를 인증 EP 4종에 적용.

### P0-4. prod env 확인 2건 [OPS·확인만]
- `backup_dir` 미설정이면 **백업 자체가 비활성**(scheduler.py 게이트) — 즉시 확인. `trusted_hosts` — config 주석 "운영에선 반드시 설정"(open redirect 방어). 미설정 시 env 주입만으로 해결(코드 불변).

### P0-5. FE: error.tsx / not-found.tsx 부재 [FE·HIGH·S]
- 현상: `src/app/`에 error/not-found/loading 0개 — 렌더 크래시 시 Next 기본 빈 화면(복구 경로 없음).
- 조치: 루트+`(app)` error.tsx("다시 시도"/대시보드 복귀)·not-found.tsx.

### P0-6. FE: ~20개 화면 API 에러 시 "빈 데이터" 오인 [FE·HIGH·M]
- 현상: `useQuery` isError 미처리 화면 ~20개(hr/payroll·tax·compliance·finance-cash-forecast 등)가 403/500에서 **"데이터가 없습니다"로 정상 표시** — 세무/마감 화면에서 위험한 오해.
- 조치: `<QueryGuard>` 공용 래퍼 1개(에러 배너는 기존 ~25화면 패턴 재사용) + `e.status` 분기(403 권한/5xx 재시도/네트워크) → P2-8 로딩 상태와 동일 래퍼로 일괄 해결.

### P0-7. FE: 미인증 리다이렉트 루프 의심 [FE·MED·S]
- 현상: `lib/api.ts` 401→`/erp/` 리다이렉트인데 루트 페이지가 `/dashboard`로 재리다이렉트 → 세션 만료 시 루프 가능(nginx SSO 인터셉트 여부 prod 확인 필요). kiosk(ws) 페이지도 PIN 만료 시 SSO 랜딩으로 튕김.
- 조치: LANDING을 `/login`으로(로그인 페이지 존재·PIN+패스키+비번 완비) 또는 루트를 bootstrap-aware로. nginx `/erp/` 동작 선확인.

### P0-8. 문서 stale 정리 [DOC·S]
- `backlog.md`·`erp-to-his-requests.md`의 cssd.po.approved 항목이 "미구현"으로 남아있으나 **이미 구현·등록 확인됨** — 갱신. `infra/.env.coloc.example` 파일 부재(문서가 참조) 복원.

### P0-9. UX-0 기능결함 8건 [FE+BE·HIGH·S~M] ★UX 심층 조사 신규
- UX 감사에서 발견됐으나 성격은 **버그**(데이터 오염·내부통제·업무 완결 불가). 상세·수정안 = `features/ux-ui-improvement.plan.md` §1:
  - **X0-1 접수 진료과 "IM" 하드코딩**(`patients/page.tsx:48`) — **prod 데이터 오염 진행 중**(부서 RBAC·부문손익·삭감통계 왜곡). 최우선.
  - X0-2 수납 수단 CARD 고정(현금 수납 불가·일마감 수단별 분개 왜곡) · X0-3 전표 승인 화면 금액/라인 미표시(내부통제) · X0-4 세금계산서 대사 완결 불가(미지급 ID 미표시) · X0-5 마감 정정 dead limb · X0-6 마약류 하향 조정 불가 · X0-7 고아 화면 2종(`/admin/payroll-settings`·`/assets/my`) · X0-8 도메인 관리자 기본 메뉴 결핍(자기 2FA/급여명세 접근 불가·BI 링크 FINANCE_MANAGER 누락).

---

## 2. P1 — 재발 방지 테스트 안전망 (2~3주)

> 근거: 이번 분기 prod escape 2건(2FA kwarg 전면 500·admin billing 권한 오분류)은 모두 **HTTP 계층 무테스트**가 직접 원인. 서비스 함수 직호출 테스트(1213개)로는 DI·인증·라우터 시그니처·422를 못 잡는다.

### P1-1. HTTP 계층 테스트 도입 [TEST·HIGH·M]
- 현상: 496 엔드포인트, TestClient/ASGITransport 사용 0건.
- 조치: `ASGITransport(app=create_app())` httpx 픽스처+인증 헤더 팩토리 1회 구축 → 라우터당 happy-path 1+403 1+422 1(약 25 라우터). 2FA·admin billing 회귀를 첫 케이스로.

### P1-2. RBAC 매트릭스 스냅샷 테스트 [TEST·HIGH·S~M]
- 현상: test_policy.py는 사후 스팟체크 10개 — WORKSTATION 과다부여(v1.71)가 그래서 escape.
- 조치: `create_app().routes` 인트로스펙션 → 라우트별 (resource, action) 선언 덤프를 리뷰된 매트릭스 파일과 스냅샷 비교 + "kiosk는 scm 외 전부 거부" 속성 체크.

### P1-3. 워커 잡 테스트 [TEST·HIGH·S~M]
- 현상: workers/scheduler.py 604 LOC ~22잡 무테스트. **법정 접속기록 delete-after-commit(v1.65 HIGH 수정)** 순서가 주석으로만 존재.
- 조치: FakeRedis(기존 test_his_webhook 패턴)로 xdel-after-commit 순서 검증 + build_scheduler() 잡 레지스트리 스냅샷 + _startup_catchup 멱등 판정 테스트.

### P1-4. 모델↔마이그레이션 드리프트 체크 [TEST·MED·S]
- 현상: 단위 테스트는 create_all, CI는 빈 DB upgrade — 둘을 비교하는 스텝 없음(메모리에 실드리프트 이력).
- 조치: CI에 `alembic check`(autogen diff 비어야 통과) 스텝 추가.

### P1-5. downgrade 왕복 CI [OPS·MED·S]
- 조치: CI에 `upgrade head → downgrade -1 → upgrade head` (최근 N개) 추가. P2-2 롤백 절차의 전제.

### P1-6. core 의존성 락파일 [OPS·MED·S]
- 현상: pyproject 전부 `>=` 개방 — 빌드마다 의존성 부동·롤백 재현 불가.
- 조치: uv/pip-tools로 requirements.lock 생성·Dockerfile 사용 + CI에 pip-audit.

---

## 3. P2 — 운영 성숙·품질 부채 (1~2개월)

| ID | 항목 | 영역 | 리스크 | 노력 | 요지 |
|---|---|---|---|---|---|
| P2-1 | 오프사이트 백업 반출 | OPS | HIGH | M | 백업이 DB와 같은 디스크 — rclone/scp 반출 잡+성공 검증(암호화는 기존) |
| P2-2 | 배포 롤백 아티팩트·절차 | OPS | HIGH | M | 배포 스크립트화: `docker tag :prev` → health 스모크 실패 시 prev 재기동 + runbook §3 롤백 절차 + rsync `--backup-dir` |
| P2-3 | 메트릭/에러 경보 | OPS | HIGH | M | starlette-exporter(/metrics) → 기존 Grafana에 시스템 대시보드·알람(HIGH지만 P0-1/2가 급소를 먼저 막음) |
| P2-4 | 공유 PG 이웃 감시 자동화 | OPS | HIGH | S | runbook §9.1 진단 쿼리를 cron+경보로(근본=DB 분리는 백로그 유지) |
| P2-5 | 시크릿 파일 주입 복귀·분리 | SEC | MED | S~M | .env 단일 평문 6키 → docker secrets 파일 주입(코드 지원 기존·Dockerfile 권한만 수정)·키 파일 분리 |
| P2-6 | 키 회전 절차·도구 | SEC | MED | M | crypto 버전→키 맵 확장+재암호화 커맨드·백업 키버전 메타·회전 runbook 1페이지(문서 먼저) |
| P2-7 | PG 전체 테스트 레인 | TEST | MED-HIGH | M | session 픽스처를 DATABASE_URL 파라미터화 → 1213 전체를 CI Postgres에서(nightly)·JSONB/EXCLUDE/schema 실검증 |
| P2-8 | 동시성 레인 확장 | TEST | HIGH | M | with_for_update 18사이트 중 PG 레이스 테스트 1개뿐 — 기존 asyncio.gather 패턴 복제로 사이트당 1개(approval·stocktake·incentive·forex 우선) |
| P2-9 | FE 로딩 상태(가짜 빈 테이블) | FE | MED | M | Table `loading` prop+Spinner — P0-6 QueryGuard와 동일 래퍼 → **UX 기획 R1에 흡수** |
| P2-10 | FE RBAC 정합(BI 링크·role 하드코딩) | FE | MED | S | BI 링크 누락은 **P0-9(X0-8)로 승격** · `role==="ADMIN"` 5사이트 → `/menu` 기반(UX 기획 X4) |
| P2-11 | FE 페이지네이션(size=50 절단) | FE | MED | M | 전표/로그/알림 10화면 — **UX 기획 X3-4(Table v2)에 흡수** |
| P2-12 | ITAM·tax·costing 테스트 보강 | TEST | MED | M | itam 2.6k LOC/47테스트(PHI게이트·폐기결재·GL·Shadow-IT 분할)·tax 14·costing 10 보강 + 전역 차대균형 스윕 픽스처 |

---

## 4. P3 — 내부 잔여·정밀화 (분기 내)

| ID | 항목 | 근거 | 리스크 | 트리거/시한 |
|---|---|---|---|---|
| P3-1 | 간이세액표 정밀 적재 | hr/statutory.py 근사식 — 과다원천징수 | MED | **연말정산 시즌 전** |
| P3-2 | 주민번호 보안채널 확정→국세청 제출파일 | tax/service.py 보류 | MED | **연말정산 시즌 전**(법무 L-2와 함께) |
| P3-3 | 공휴일 캘린더 | billing_classify.py 일요일만 인식 — 공휴일 가산 분류 오류 | MED | 데이터 확보 즉시(공공 API/정적 테이블) |
| P3-4 | drug_matcher 용량 단위 파싱 | `%200%` ILIKE — mg/mcg 무구분 | MED | 단독 수정 가능 |
| P3-5 | 백업 세대 계층(월12·연5) | common/backup.py 후속 주석 — 장기 복구점 부재 | MED | P2-1과 함께 |
| P3-6 | 급여 확정 후 라인 변경 재전기 | employee_service.py 후속 | MED | 정책 결정 후 |
| P3-7 | physician-revenue 직접원가 배부 | direct_cost=0 고정 | MED | 원가배부 정책(P-3 D42)과 함께 |
| P3-8 | FE 정리 배치 | **UX 기획 R1(토스트·확인)·R4(폼·상태색·스캐폴딩)로 재편** — MoneyInput·confirm() 대체·format.ts·aria 포함 | LOW-MED | UX 기획 로드맵 준수 |
| P3-9 | FE Playwright e2e | 상위 15화면·시드 dev 백엔드 대상 — **UX R2(URL 상태) 이후가 최적**(딥링크로 시나리오 작성 가능) | HIGH가치·L노력 | P1-1·UX R2 이후 |
| P3-10 | 시간 고정 테스트(time-machine) | 월경계·KST 반복 결함 이력 | MED | freezegun류 도입+frozen_kst 픽스처 |

**수용(수정 안 함·기록)**: v1.69 LOW 3건(terminate reject 유도·filter_gate 과차단=안전방향·audit 2-replica)·worklist 롤업 SQL(의미변경 위험>이득)·현금흐름 활동분배 근사·forward-looking ECL(회계사 영역)·사전심사 판정보류(의도적 오탐 억제).

---

## 5. EXT — 외부 의존 트랙 (코드 무관·조율 산출물)

### 연쇄 병목 (최우선 추진)
```
E-1 심평원 MEDRLT 실물 샘플 2건(M0c)
 └→ I-2 UNA decimal 확정 · I-3 한정자 검증
     └→ G-1 medrlt_live 게이트 ON (활성 전 결함은 v1.62·v1.23·#788/#789로 全수정)
         └→ W-1 실삭감 라벨 → weve P0.5 섀도 캘리브레이션 → P1 노출
             └→ P-4 룰 적중률 자동산출 · 지급대사 완전 종결
```

### HIS팀 (요청서 발송/회신 대기)
| ID | 항목 | 상태 | 비고 |
|---|---|---|---|
| H-4 | **billing.adjusted에 결제수단(method) 필드** | **요청서 미발송 — 신규 작성 필요** | 카드환불 1132 정합(I-5)의 유일 차단자 |
| H-1 | billing.line_recorded에 diagnosisCodes | ERP 수신측 全 LIVE·HIS 송신 대기 | 사전심사 의학적 타당성 |
| H-2 | 약품 코드 매핑 11건 회신 | 확정 화면 운영 중 | |
| H-5 | 신규 consumer 실 이벤트 E2E 각 1건 | 합성으로만 검증됨 | |
| H-9 | R-10 증명서 정본 통일 | 결정 대기 | |

### 회계사/법무/비즈니스 결정
- **A-1 환자 발생주의 사인오프**(패킷 #654 발송됨) → G-2 게이트 ON — 활성 전 결함 全수정 상태(v1.22·v1.70)
- A-2 1차 준거(의료기관 회계기준 vs K-IFRS) · A-4 IFRS 부분충족 3건(이연법인세·DBO·차입원가=전문가 영역)
- L-2 주민번호 채널(P3-2 연동) · L-3 검진권 딜러 DB-1/2/3(원천징수율·환율·계정) → G-5·I-6 차단 해제
- G-6 prereview_auto_dismiss 옵트인 — 운영량 판단 후

### weve AI팀
- W-3 B-3 화이트리스트 → sync_verify push 스텁(I-1) 해소 · W-2 A4/A5 초안 EP

---

## 6. 실행 순서 제안 (주 단위)

- **W1**: P0 전체(9건 — env 확인 2건은 즉일·**P0-9 X0-1 접수 dept 하드코딩 최우선**) + H-4 요청서 발송
- **W2~3**: P1 전체(HTTP 픽스처가 축 — P1-1 구축 후 P1-2/3 병행·P1-4/5/6은 CI 소규모)
- **W4~7**: P2 — 운영(P2-1/2/3/4) 트랙과 테스트(P2-7/8/12) 트랙 병행, **FE는 UX 기획 R1~R3**(토스트/확인 → URL 상태/픽커/참조내비 → 알림 딥링크/결재함/지급대사 화면)
- **W8+**: P3 — 연말정산 시한물(P3-1/2) 우선, **FE는 UX 기획 R4~R6**(상태색/폼/스캐폴딩 → IA → Table v2/라우트화/화면별 심화·급여 최우선)·e2e
- **상시**: EXT 트랙 — E-1 샘플 확보 추진이 전체 백로그의 최대 지렛대

## 7. 완료 기준
- P0: prod에서 백업 실패·워커 사망이 24h 내 경보로 도달 / 인증 EP 스로틀 확인 / FE 에러·크래시 화면 정상
- P1: 신규 PR에 라우터 추가 시 HTTP 테스트 없으면 리뷰 반려 가능한 상태 / CI에 drift·downgrade·audit 스텝 green
- P2: 복구 리허설 분기 1회 runbook 명문화·오프사이트 1일 1회 검증 / 1213+ 전체가 PG에서 nightly green
- 각 티어 종료 시 본 문서 갱신(완료 표기·신규 발견 편입)
