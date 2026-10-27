# 배포·롤백 런북 (P2-2)

> 대상: ERP 동거 배포(HIS 서버 co-location)의 버전 태그 배포 + 원클릭 롤백. 2026-07-15.
> 도구: `infra/deploy.sh` · `infra/rollback.sh` · `infra/docker-compose.coloc.yml`(`image: infra-*:${ERP_VERSION}`).
> 실행 위치: 운영 호스트의 저장소 사본 디렉터리(아래 예시 경로는 기관 환경에 맞게 바꿉니다).

## 1. 설계 요약

- compose 서비스(core/web/worker-batch/worker-integration)에 `image: infra-<svc>:${ERP_VERSION:-latest}` 부여.
  `ERP_VERSION`을 주고 빌드하면 이미지가 **버전 태그로 보존**된다(예: `infra-core:v1.125.0`).
- 새 배포는 새 태그를 만들 뿐 **구 태그 이미지를 덮어쓰지 않는다** → 언제든 이전 버전으로 재기동 가능.
- `deploy.sh`가 세대보관(기본 최근 5개·`ERP_IMAGE_KEEP`)하며 초과분만 정리.
- 롤백은 **재빌드 없이**(`up -d --no-build`) 보존된 구 이미지를 기동 → 초 단위 복구.

## 2. 배포

```bash
cd /home/deploy/his-backup/weruby/hospital-erp
# 소스 동기화(개발 머신에서 rsync 후) — 또는 CI 산출물 배치
./infra/deploy.sh v1.125.0                 # 전체(core web worker-batch worker-integration)
./infra/deploy.sh v1.125.0 core            # 코드영역만 바뀐 경우 일부만
```
- 빌드→기동→**core 헬스 검증**(실패 시 비정상 종료·운영자 롤백 판단)→구 이미지 세대보관.
- 완료 후 가용 롤백 대상(버전 태그 목록)을 출력.

> 마이그레이션은 core Dockerfile CMD가 기동 시 자동 적용 — v1.289.0 이상은 `python -m erp.migrate_on_start`
> (DB가 이미지보다 앞서 있으면 upgrade 생략), v1.288.0 이하는 `alembic upgrade head`.
> 배포 게이트는 `/api/health/deep`의 `checks.db`·`checks.redis`만 본다(워커 하트비트·정기업무 신선도는
> 게이트 밖 — 최초 설치·워커 장애 시 배포가 core만 올린 채 멈추던 문제, 2026-10-09 전체 흐름 검토 2차 H).
> core 외 서비스는 `up -d --no-deps`로 올려 검증된 core를 다시 만들지 않는다.

## 3. 롤백

```bash
cd /home/deploy/his-backup/weruby/hospital-erp
./infra/rollback.sh                 # 가용 롤백 대상 목록
./infra/rollback.sh v1.124.0        # 해당 버전으로 롤백(확인 프롬프트)
./infra/rollback.sh v1.124.0 -y     # 무확인(자동화)
```

### ★ 마이그레이션 있는 릴리즈의 롤백 (중요)

> 2026-10-09 전체 흐름 검토 2차 H — **롤백 대상 이미지 세대에 따라 절차가 다르다.** `rollback.sh`가 대상
> 이미지 CMD를 보고 어느 쪽인지 출력한다(`docker image inspect infra-core:<ver> --format '{{json .Config.Cmd}}'`).

| 롤백 대상(구) 이미지 | core CMD | 마이그 추가 릴리즈를 되돌릴 때 |
|---|---|---|
| **v1.289.0 이상** | `python -m erp.migrate_on_start` | DB 리비전이 이미지에 없으면 upgrade를 **건너뛰고 기동** → `rollback.sh <버전>`만으로 **코드만 롤백**(데이터 보존). 다운그레이드는 신 스키마가 구 코드를 실제로 깨뜨릴 때만. |
| **v1.288.0 이하** | `alembic upgrade head && uvicorn` | 구 alembic이 현재 리비전을 몰라 **기동 실패** → **먼저 수동 다운그레이드** 후 롤백. |

코드영역만 바뀐(마이그 불변) 릴리즈는 어느 세대든 `rollback.sh <이전버전>`만으로 안전.

v1.288.0 이하로 되돌리며 다운그레이드가 필요한 경우:
```bash
# 1) 현재 DB 리비전 확인
docker exec infra-core-1 alembic current
# 2) ⚠ 다운그레이드가 지울 데이터를 먼저 백업
#    0251 downgrade = hr.employees.access_revoked_on 삭제 → 퇴사자 접근차단 표식 소실
#                     (다시 올리면 일일 잡이 과거 퇴사자를 '처음 보는' 대상으로 재판정)
#    0250 downgrade = hr.statutory_params DROP → 관리화면에서 등록한 법정 요율 소실(코드 상수로 회귀)
docker exec docker-postgres-1 pg_dump -U hospital -Fc erp -t hr.statutory_params \
  > /home/deploy/backup/statutory_params-$(date +%F-%H%M).dump
docker exec docker-postgres-1 psql -U hospital -d erp -c \
  "\copy (select id, access_revoked_on from hr.employees where access_revoked_on is not null) to stdout csv header" \
  > /home/deploy/backup/access_revoked_on-$(date +%F-%H%M).csv
# 3) 롤백 대상(구 이미지)이 아는 리비전으로 다운그레이드 (예: v1.287.3 → head 0247)
docker exec infra-core-1 alembic downgrade 0247
# 4) 구 이미지로 롤백
./infra/rollback.sh v1.287.3
```
> ⚠ `alembic downgrade`는 **데이터 손실 가능**(컬럼/테이블 삭제). 되도록 additive 마이그는 그대로 두고
> **코드만 롤백**하는 편이 안전하다 — v1.289.0 이상 이미지끼리는 그게 기본 동작이다. 애매하면
> 다운그레이드 대신 hotfix 전진 배포를 고려. 백업에서 복원할 때는 `pg_restore -t`/`\copy from`.

## 4. 주의 · 한계

- **롤백 앵커는 배포 시점부터 축적**된다. `deploy.sh`(ERP_VERSION 지정)로 배포해야 태그가 남는다.
  과거 `up -d --build`(버전 미지정) 배포분은 `latest`만 있어 롤백 대상이 아니다.
- 세대보관은 로컬 이미지 기준 — 호스트 재프로비저닝 시 소멸(이미지 레지스트리는 P2 별도 백로그).
- 롤백은 **애플리케이션 코드/이미지** 복구만 담당. 데이터·마이그는 위 §3 절차로 별도 판단.
- 오프사이트 백업(P2-1)·docker secrets 전면전환(P2-5)은 별도 항목.
