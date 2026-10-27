# ERP v1.118.0 → v1.126.0 릴리스 — P2 운영 인프라(관측성·회복탄력성·보안 운영)

> **Summary**: 운영 인프라 계획(`full-system-remediation.plan.md` P2)의 **코드 항목 전부 + 인프라결정 2건**을
> 완주. **PR #837~#847 · 9 릴리스 · 마이그 0196 → 0197 · 테스트 1256 → 1271 · prod 다회 배포 전부 검증 LIVE**.
> 앱 계층 관측성(메트릭)·실 PG 테스트 레인·동시성 실검증·키 회전·이웃 슬롯 감시·회계 불변식 테스트·버전
> 태그 롤백 배포·파일 기반 시크릿까지. **PG 테스트 레인이 sqlite가 가리던 실 prod 스키마 결함 3종 + TZ
> 버그를 즉시 적발·수정**한 것이 최대 성과.
>
> **A 코드항목(P2-3·7·8·6·4·12) 완주 + 인프라결정 P2-2 롤백 완료 · P2-5 코드절반 완료.**

**연관 계획**: `docs/01-plan/full-system-remediation.plan.md`(P0~P3+EXT).

---

## 1. 배경

기능은 심화됐으나 **운영 인프라**가 약했다: 앱 계층 메트릭 0(Grafana가 DB만 관측), 단위 테스트가
sqlite에서만 돌아 PG 방언 회귀를 못 잡음, 행잠금은 코드에만 있고 실검증 부재, 암호화 키 회전 수단 없음,
공유 PG 슬롯 고갈은 관리자가 눌러야 보이는 온디맨드 진단, 배포는 태그·롤백 없는 `up --build`, 시크릿은
env 노출. P2는 이 운영 갭을 코드 항목부터 순차 해소했다.

## 2. 릴리스별 요약

| 릴리스 | 항목 | 핵심 | 마이그 |
|---|---|---|---|
| **v1.118.0** #837 | P2-3 메트릭 | 의존성 없는 Prometheus `/api/metrics`(요청 카운터·지연·헬스 게이지·라우트 템플릿 라벨) | 불변 |
| **v1.119.0** #838 | P2-7 PG 레인 | `conftest`를 `TEST_DATABASE_URL`로 분기(외부 트랜잭션 롤백 격리)+CI nightly 비차단 레인 | 불변 |
| **v1.120.0** #839 | P2-7 결함수정 | **컬럼이 자기 enum값 못 담던 3종**(role 16<17·status 10<11·waste 10<11) 확대·출퇴근 TZ·테스트 격리 | **0197** |
| **v1.120.1** #840 | P2-7 보정 | 출퇴근 시각 벽시계 보존(`astimezone` 환산 오류→tzinfo 제거로 정정) | 불변 |
| **v1.121.0** #841 | P2-8 동시성 | 승인 이중승인·재고 FIFO 과다출고·외화 채번경합 행잠금 실검증(PG 전용) | 불변 |
| **v1.122.0** #842 | P2-6 키회전 | 버전 키맵·`reencrypt_column`·재암호화 CLI(10컬럼)·4단계 무중단 런북 | 불변 |
| **v1.123.0** #843 | P2-4 이웃감시 | 공유 PG 슬롯 점유 능동 감시 잡(10분·임계 알림·Redis 디둡) | 불변 |
| **v1.124.0** #844 | P2-12 테스트 | 전역 차대균형 불변식 + ITAM 폐기(PHI 게이트·결재) 커버리지 | 불변 |
| **v1.125.0** #845 | P2-2 롤백 | 버전 태그 이미지·`deploy.sh`·`rollback.sh`·마이그 롤백 런북 | 불변 |
| **v1.126.0** #847 | P2-5 시크릿 | 파일 기반 시크릿 로딩(암호화/bidx 키까지·하위호환) | 불변 |

(#846 = rollback.sh 목록 grep 정정)

## 3. 핵심 성과 — PG 레인이 실 prod 결함을 적발

P2-7 레인 첫 실행에서 **1246/1256 통과, 10 실패** — sqlite가 가리던 실제 결함:

- **컬럼이 자기 유효값을 못 담던 3종**(PG는 varchar 길이 강제): `users.role`/`menu_permissions.role`
  `String(16)` < `LOGISTICS_MANAGER`(17자)·`contingencies.status` `String(10)` < `PROVISIONED`(11자)·
  `medical_waste_logs.status` `String(10)` < `HANDED_OVER`(11자·CHECK 허용값인데). **prod에서 정상 enum
  값 INSERT가 에러나던 잠복 결함** → 마이그 0197로 확대(prod 32/16/16 실측).
- **출퇴근 naive↔aware TZ 버그**(PG `timestamptz`=UTC-aware·sqlite=naive) → `as_naive_wallclock`로 통일.
- 테스트 격리 결함(PG FK 강제·IDENTITY 시퀀스 롤백 비리셋).

전부 수정 후 **최종 PG 레인 1271 passed·0 fail**. 이후 nightly 비차단 레인으로 상시 회귀 감지.

## 4. prod 검증(발췌)

- `/api/metrics`: `erp_up 1`·`db_up 1`·`redis_up 1`·worker 하트비트·요청 카운터 live.
- 동시성: CI 백엔드잡 `tests/integration` 5 passed(행잠금 실직렬화).
- 키회전 도구: prod dry-run — patient phone 333·employee phone 285·comp 251 등 전부 `[v1:*]`·재암호화 0.
- 이웃감시: `shared_pg_stats` usage 39%(78/200)·**openpacs=62**(과거 인시던트 주범 실관측)·worker 잡 등록.
- 롤백: `deploy.sh`로 v1.125·v1.126 태깅→앵커 2개·`rollback.sh` 목록 렌더·헬스 게이트 동작.
- 시크릿: 파일 로딩 확장 후 암호화 라운드트립 정상(key_ver=1·env 로딩 무변경).

## 5. 잔여 (순수 결정·외부 의존)

- **P2-5 활성화**: 소스 파일 `0444`(공유박스 노출) ↔ docker inspect 노출 제거 트레이드오프 — 보안 정책 판단.
  (M-2 재검증: compose native secrets가 소스 0600 보존→uid 10001 못 읽음·앱 env 폴백 중.)
- **P2-1 오프사이트 백업**: 목적지(국내 오브젝트 스토리지/별도 SSH/관리형)+자격증명 결정 → `job_backup` 업로드 단계.
- 참고: `/api/metrics` 내부망 제한은 nginx(운영자 수동·weruby 무비번 sudo).

## 6. 산출물

- 코드: `app/metrics.py`·`app/db_diagnostics_service.py`·`common/kst.as_naive_wallclock`·`common/crypto`(버전 키맵).
- 스크립트: `scripts/rotate_data_keys.py`·`infra/deploy.sh`·`infra/rollback.sh`.
- 런북: `data-key-rotation`·`rollback-deploy`·`secrets-provisioning`(전부 `docs/05-operations/`).
- 테스트: PG 레인(nightly)·`tests/integration` 동시성 3종·`test_global_ledger_balance`·`test_itam_disposal`·`test_kst_naive`·`test_secret_files`.
