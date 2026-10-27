# 백업 복원 런북 (v1.288.1~)

> 근거: 2026-10-09 전체 흐름 검토 H — 종전 백업은 스키마 9개 고정 목록이라 `itam`·`reporting`·`alembic_version`·
> 확장이 빠져 **복구가 성립하지 않았다**. v1.288.1부터 ERP DB 전체 + 첨부 tar를 같은 키로 암호화한다.
> ⚠ 복원은 **새 DB에 먼저** 해 보고 확인한 뒤 전환한다. 운영 DB에 바로 덮어쓰지 않는다.
>
> 2026-10-09 전체 흐름 검토 2차 H (v1.289.0~):
> - **파일 포맷 v2** — 평문을 메모리에 들지 않는 스트리밍·청크(16MiB) 인증 암호화. 파일 첫 7바이트가
>   `ERPBK2\n`이면 v2, 아니면 v1(레거시 단일 블롭). 복호 CLI는 **두 포맷 모두 자동 판별**한다.
>   v2는 프레임 절단·재배열·접합을 복호 실패로 검출한다(잘린 백업이 '성공'으로 복호되지 않음).
> - **권한(GRANT/REVOKE) 포함** — 종전 `pg_dump --no-privileges`는 원장 불변성 REVOKE(voucher_lines 0192·
>   narcotics_ledger 0004·approval_ledger 0203·audit_log 0225)와 `erp_bi` GRANT를 빼서, 복구본에선 erp_app이
>   원장을 고칠 수 있었다. 이제 포함하며 §3-1에서 반드시 확인한다. 대상 인스턴스에 `erp_bi`·`erp_app` 롤이
>   없으면 GRANT 줄에서 멈춘다(ON_ERROR_STOP) → 롤을 먼저 만든다.
> - 오프사이트(설정 시)로 DB 파일과 **첨부 파일 둘 다** 전송한다(종전엔 DB만).

## 1. 파일
- `/data/backup/erp-YYYY-MM-DD.sql.enc` — DB 전체(pg_dump 평문 SQL을 AES-256-GCM)
- `/data/backup/erp-attachments-YYYY-MM-DD.tar.gz.enc` — 첨부 볼륨(서명 완료본·증빙)
- 키: `BACKUP_ENC_KEY`(없으면 `DATA_ENC_KEY`). **키는 백업 매체와 분리 보관**해야 한다 — 같은 호스트의
  `infra/.env`에만 있으면 호스트 소실(9/9형) 때 백업과 키를 함께 잃는다.

## 2. 복호화
```bash
docker exec infra-worker-batch-1 python -m erp.common.backup decrypt \
  /data/backup/erp-2026-10-09.sql.enc /tmp/erp-restore.sql
docker exec infra-worker-batch-1 python -m erp.common.backup decrypt \
  /data/backup/erp-attachments-2026-10-09.tar.gz.enc /tmp/att.tar.gz
docker cp infra-worker-batch-1:/tmp/erp-restore.sql . && docker cp infra-worker-batch-1:/tmp/att.tar.gz .
```

## 3. 새 DB에 복원(리허설)
```bash
docker exec docker-postgres-1 psql -U hospital -d postgres -c "CREATE DATABASE erp_restore OWNER erp_app"
# pg_dump 17로 만든 덤프를 PG16에 넣으면 `SET transaction_timeout` 한 줄이 오류 — 무시해도 된다(또는 sed로 제거)
sed -i '/^SET transaction_timeout/d' erp-restore.sql
docker exec -i docker-postgres-1 psql -U erp_app -d erp_restore -v ON_ERROR_STOP=1 < erp-restore.sql
docker exec docker-postgres-1 psql -U erp_app -d erp_restore -Atc "select version_num from alembic_version"
docker exec docker-postgres-1 psql -U erp_app -d erp_restore -Atc "select count(*) from itam.assets"   # itam 포함 확인
```

### 3-1. 권한(불변성) 확인 — 필수
원장 4종에 erp_app의 UPDATE·DELETE가 **모두 `f`**여야 한다. 하나라도 `t`면 그 복구본은 불변성이 깨진
상태다 — 전환하지 말고 해당 마이그레이션의 REVOKE를 재적용한 뒤 다시 확인한다.
```bash
docker exec docker-postgres-1 psql -U hospital -d erp_restore -Atc "
select t, has_table_privilege('erp_app', t, 'UPDATE') as upd, has_table_privilege('erp_app', t, 'DELETE') as del
from unnest(array['finance.voucher_lines','scm.narcotics_ledger','common.approval_ledger','audit.audit_log']) t"
# 기대: 4행 모두 '|f|f'
docker exec docker-postgres-1 psql -U hospital -d erp_restore -Atc "
select count(*) from information_schema.role_table_grants where grantee='erp_bi'"   # BI GRANT 복원(>0)
```

### 3-2. 평문 삭제 — 필수 (§4 첨부 복원까지 끝낸 직후)
복호한 평문(SQL·tar)은 환자·급여 데이터 원문이다. 복원 확인 직후 컨테이너와 호스트 양쪽에서 지운다.
```bash
docker exec infra-worker-batch-1 shred -u /tmp/erp-restore.sql /tmp/att.tar.gz
docker exec infra-core-1 shred -u /tmp/att.tar.gz   # §4에서 복사한 경우
shred -u erp-restore.sql att.tar.gz
```
> 컨테이너에 `shred`가 없으면 `rm -f`(coreutils 미포함 이미지). 복호 CLI 출력은 0600·기존 파일 덮어쓰기 거부.

## 4. 첨부 복원
```bash
docker cp att.tar.gz infra-core-1:/tmp/
docker exec -u 0 infra-core-1 sh -c 'cd /data && tar xzf /tmp/att.tar.gz && chown -R 10001:10001 /data/attachments \
  && find /data/attachments -type d -exec chmod 2775 {} + && find /data/attachments -type f -exec chmod 664 {} +'
docker exec infra-core-1 python -m erp.attachment_check     # 본문 없는 첨부 0건
```

## 5. 전환
`.env`의 `DATABASE_URL` DB 이름을 바꾸고 `ERP_VERSION=<현재 태그> docker compose -f infra/docker-compose.coloc.yml up -d --force-recreate core worker-batch worker-integration`.

## 6. 정기 드릴
분기 1회 §3까지 수행하고 결과(행 수·리비전)를 운영 기록에 남긴다. 복원해 본 적 없는 백업은 백업이 아니다.
