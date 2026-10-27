# BI reporting 읽기전용 롤 런북 (erp_bi)

> **상태**: P0 마트 LIVE 후 BI 도구 연결 직전 1회. **슈퍼유저 `hospital` 작업**(erp_app은 CREATE ROLE
> 권한 없음 → 마이그가 아니라 본 런북). 공유 `docker-postgres-1`.
> 기획서: [bi-tool-integration.plan.md](../01-plan/features/bi-tool-integration.plan.md) §4·§7·§9.

## 목적
BI 도구(Grafana/Metabase)가 운영 테이블 직결 없이 **`reporting` 스키마(집계·PII 없음)만** 읽도록
전용 읽기전용 롤 `erp_bi`를 만든다. 운영 롤 `erp_app`·운영 스키마와 권한 분리.

## 선결
- 마이그 0145 적용(스키마 `reporting` + `mart_exec_kpi_monthly` 생성).
- 야간 잡 `job_refresh_bi_marts`로 마트가 채워짐(또는 수동 1회 갱신).

## 실행 (슈퍼유저 hospital)
```sh
# 1) 롤 생성 — 로그인 가능·약한 격리·커넥션 캡(공유 PG 슬롯고갈 방어, ops-runbook §9.1)
docker exec docker-postgres-1 psql -U hospital -d erp -c \
  "CREATE ROLE erp_bi LOGIN PASSWORD '<강한_시크릿>' CONNECTION LIMIT 10;"

# 2) reporting 스키마만 SELECT — 운영 스키마 GRANT 없음(직결 차단)
docker exec docker-postgres-1 psql -U hospital -d erp -c "GRANT USAGE ON SCHEMA reporting TO erp_bi;"
docker exec docker-postgres-1 psql -U hospital -d erp -c \
  "GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO erp_bi;"
# 향후 추가 마트도 자동 SELECT — default privileges (소유자=마이그 실행 롤 erp_app 기준)
docker exec docker-postgres-1 psql -U hospital -d erp -c \
  "ALTER DEFAULT PRIVILEGES FOR ROLE erp_app IN SCHEMA reporting GRANT SELECT ON TABLES TO erp_bi;"

# 3) (방어) 운영 스키마 차단 확인 — 명시 REVOKE는 불필요(GRANT 안 했으면 접근 불가)하나 점검
docker exec docker-postgres-1 psql -U hospital -d erp -c \
  "SELECT has_schema_privilege('erp_bi','finance','USAGE') AS finance_usage;"   # → f (false)
```

## 검증
```sh
# erp_bi로 reporting 읽기 OK
docker exec docker-postgres-1 psql -U erp_bi -d erp -c \
  "SELECT yyyymm, revenue FROM reporting.mart_exec_kpi_monthly ORDER BY yyyymm DESC LIMIT 3;"
# 운영 스키마는 거부(permission denied)
docker exec docker-postgres-1 psql -U erp_bi -d erp -c "SELECT 1 FROM finance.vouchers LIMIT 1;"  # → ERROR
```

## BI 도구 연결
- datasource: host `docker-postgres-1`(또는 서비스 DNS)·db `erp`·user `erp_bi`·schema `reporting`·**읽기전용**.
- 풀 캡: 도구측 max connections ≤ 5(롤 CONNECTION LIMIT 10 내). 공유 PG 부하 격리.
- **PII 없음**: 마트는 집계 전용 — BI는 환자/개인 식별자에 닿지 않음(컴플라이언스).

## 회전·해제
- 시크릿 회전: `ALTER ROLE erp_bi PASSWORD '<신규>';` → BI datasource 갱신.
- 해제: `REVOKE ALL ON ALL TABLES IN SCHEMA reporting FROM erp_bi; DROP ROLE erp_bi;`

## 주의
- ⛔ 운영 스키마(finance·hr·patient…) GRANT 절대 금지 — PII 암호문/RBAC 우회 방지(기획서 §3).
- 읽기 부하가 커지면 **읽기 복제본**으로 이전(기획서 §9, DB 분리 백로그 연계). 현 단계는 캡으로 격리.
