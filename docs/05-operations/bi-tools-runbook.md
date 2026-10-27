# ERP 자체 BI 도구 런북 (Grafana P1 · Metabase P2)

> 기획서 [bi-tool-integration.plan.md](../01-plan/features/bi-tool-integration.plan.md) §8 P1·P2.
> his-grafana(HIS 소유) 협조가 어려울 때의 **ERP 자체 BI**. `reporting` 마트만 읽음(erp_bi·운영/PII 무접근).
> compose: `infra/docker-compose.bi.yml`(coloc 백엔드와 별개·opt-in). **busy 박스 — 자원 캡 적용**.

> ⚠ **2026-09-09 서버 이전 — 아래 nginx 스니펫은 더 이상 실행 구성이 아니다.** 리버스 프록시가
> nginx → **Caddy**(`~/his-backup/Caddyfile`, Cloudflare Tunnel 뒤)로 바뀌었다. 규칙은 같은 것을
> 재현했다: `/erp/bi*`는 `forward_auth`로 core `/api/v1/auth/bi-check` 호출 → 통과 시
> `X-Erp-User/Role`을 **X-WEBAUTH-USER/ROLE**로 복사해 Grafana(3011)에, 401/403이면 `/erp/`로 302.
> `/erp/mb*`는 `metabase.SESSION` 쿠키 없으면 자동로그인 브리지로 302 + 같은 게이트 → Metabase(3012).
> nginx 문법 블록은 **이력·의도 설명**으로만 읽고, 손댈 때는 Caddyfile을 고칠 것.

## 선결
- 마이그 0145~0151 prod 적용(reporting 마트 **11종** + 폴러 백오프 컬럼) — 완료(alembic head 0151).
  마트: 경영KPI·진료과손익·감사·자금수지·채권채무 Aging·기능별손익·검진권·이직·재고·자금예측·연간재무비율.
- `erp_bi` 읽기전용 롤 생성 — 완료(런북 [bi-reporting-role.runbook.md](bi-reporting-role.runbook.md)).
  비밀번호 **정본은 prod `infra/.env.bi`의 `ERP_BI_PASSWORD`**(600).
  ⚠ 종전 문서가 가리키던 메모 파일 `erp-bi-secret.txt`는 **현 서버에 없고 2026-09-08 백업에도 없다**
  (같은 백업이 600 권한 `.env`·`.env.bi`는 담고 있으므로 권한 문제가 아니라 **파일 자체가 부재**).
  인용하지 말 것 — 값이 필요하면 `.env.bi`, 분실 시 `ALTER ROLE erp_bi PASSWORD` 재설정.
- **Metabase SSO 브리지 설정**(ERP core `.env`): 자동 로그인 브리지가 읽는 3키 —
  `METABASE_SSO_USER`/`METABASE_SSO_PASSWORD`(Metabase 공용계정·경영 역할 사용자만 브리지 통과)·
  `METABASE_INTERNAL_BASE`(기본 `http://erp-metabase:3000`; core가 docker_default 밖이면 도달 경로 조정).
  미설정 시 `/erp/mb/` 진입이 브리지에서 에러 → 셀프서비스 BI 사용 시 필수.

## 1. 환경 파일
```sh
cd ~/his-backup/weruby/hospital-erp/infra
cp .env.bi.example .env.bi && chmod 600 .env.bi
# ERP_BI_PASSWORD = erp_bi 롤 비번(정본 = 기존 .env.bi), GF_ADMIN_PASSWORD = 강한 값
grep -c '^ERP_BI_PASSWORD=' .env.bi   # 설정 여부만 확인(값을 터미널에 띄우지 말 것)
```

## 2. 기동 (P1 Grafana — 경량, 먼저 권장)
```sh
docker compose -f docker-compose.bi.yml --env-file .env.bi up -d grafana
docker compose -f docker-compose.bi.yml logs --tail 20 grafana   # 프로비저닝 datasource·대시보드 로드 확인
```
- 접근(POC): 로컬에서 SSH 터널 → 브라우저 `http://localhost:3011`
  ```sh
  ssh -N -L 3011:127.0.0.1:3011 deploy@203.0.113.10
  ```
  로그인 admin / GF_ADMIN_PASSWORD → 폴더 **ERP 경영** → 대시보드 **ERP 경영진 BI**(수익·재무비율·인력·감사·진료과).
- datasource·대시보드는 **자동 프로비저닝**(`bi/grafana/provisioning`·`bi/grafana/dashboards`). 추가 패널 SQL은

## 3. Metabase (P2 셀프서비스 — 별도 기능·LIVE) — `https://his.example.org/erp/mb/`
**별도 기능**: SQL 없이 질문/대시보드 작성(비개발자). Grafana와 독립. **Metabase 자체 로그인**(OSS는 헤더
SSO 미지원 — Enterprise 전용). nginx가 `/erp/mb/`를 **ERP 세션으로 게이트**(auth_request 재사용·미인증 /erp/
리다이렉트) → 통과 후 Metabase 로그인. compose `MB_SITE_URL=…/erp/mb`(서브패스·상대자산), JVM 1.28G.
```sh
docker compose -f docker-compose.bi.yml --env-file .env.bi up -d metabase   # 최초 ~1~2분(H2 메타DB init)
```
- **초기 설정 완료(API)**: 관리자 `bi-admin@hospital.example.org`(⚠ 비번 메모 파일 `metabase-admin.txt` **유실**·백업에도 없음 —
  관리자 직접 로그인이 필요하면 Metabase에서 비번 재설정. 화면 진입에 쓰는 브리지 공용계정은 core `.env`의
  `METABASE_SSO_USER`/`METABASE_SSO_PASSWORD`로 **생존**) +
  **datasource "ERP reporting"**(postgres·erp_bi·schema=reporting만, 마트 **11종** sync). 추가 설정 불요.
  ⚠ 신규 마트 추가 시 Metabase datasource **재sync** 필요(관리자 → DB → Sync schema).
- **셀프서비스 자산 프로비저닝(멱등)**: `infra/bi/metabase/provision.py` — 컬렉션 3종(경영 BI·재무·운영·
  감사·리스크)·**모델 11종**(마트별 한글 컬럼 alias, type=model)·**Metrics 4종**(월평균 수익/순이익·평균
  이직률·총 미사용검진권)·**Segments 3종**(적자 진료과·보고부문 진료과·미검토 감사월)·큐레이션 질문 재배치.
  Metabase 메타DB는 git 밖 → **이 스크립트가 SoT**(신규 마트 시 MODELS/METRICS/SEGMENTS에 추가 후 재실행).
  실행: `scp provision.py → prod`, `MB_ADMIN_CRED_FILE=<자격파일> python3 provision.py`(파일 첫/끝 토큰 = 계정/비번;
  ⚠ 종전 기본 경로의 `metabase-admin.txt`는 유실 상태라 **직접 만들어 지정**해야 한다). 이름 존재 시 생략(재실행 안전).
- nginx 라우트 — **핵심: `/erp/mb` 프리픽스를 스트립**(`proxy_pass` 끝에 `/`):
  ```nginx
  # ⚠⚠ Metabase OSS 서브패스 빈 화면의 진짜 원인:
  #   프론트엔드가 base href 를 "백엔드가 받은 URI(meta[uri]) vs window.location 경로"의 차이로 계산.
  #   nginx가 /erp/mb 를 스트립 안 하면 백엔드도 /erp/mb/ 로 인식 → 차이 0 → base href="/" →
  #   자산을 루트 /app/dist/ 로 요청 → 404 → 공백. 스트립하면(proxy_pass …:3012/) 백엔드는 / 로 인식,
  #   프론트가 프리픽스 /erp/mb 검출 → base href=/erp/mb/ 정상.
  location /erp/mb/app/ {   # 정적자산(비민감·게이트 불요)
      proxy_pass http://127.0.0.1:3012/app/; proxy_set_header Host $host;
  }
  location /erp/mb/api/ {   # JSON API — ERP 세션 게이트
      auth_request /erp/bi/_auth; error_page 401 403 = @erp_bi_sso;
      proxy_pass http://127.0.0.1:3012/api/;
      proxy_http_version 1.1; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme;
  }
  location /erp/mb/ {       # 인덱스·기타 — 자동로그인 + 게이트 + 프리픽스 스트립(끝 / 필수!)
      # MB 세션 쿠키 없으면 ERP 자동로그인 브리지로(링크·직접접근·캐시 무관 자동 로그인)
      if ($http_cookie !~ "metabase\.SESSION=") { return 302 /erp/api/v1/integrations/bi/metabase-login; }
      auth_request /erp/bi/_auth; error_page 401 403 = @erp_bi_sso;
      proxy_pass http://127.0.0.1:3012/;   # ← 끝의 / 가 핵심(없으면 base href="/" → 빈 화면)
      proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";
      proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme;
  }
  ```
  > 검증: 브라우저 DOM `document.querySelector("base").href` → **`…/erp/mb/`**(`/` 면 스트립 누락=빈 화면).
  > `/erp/mb/api/session/properties` → 200 application/json. (app/api 별도 블록은 게이트 분리용·index 스트립으로도 동작.)
  > 자동로그인: ERP 인증 상태로 /erp/mb/ 진입 → (MB 쿠키 없으면) 브리지 → metabase.SESSION 주입 → 재진입 → Metabase 홈(로그인 화면 없음).
- 접근: ERP 로그인 → 사이드바 **"셀프서비스 BI ↗"**(또는 /erp/mb/ 직접) → **자동 로그인** → Metabase 홈.
  (관리자 직접 로그인 필요 시 bi-admin@hospital.example.org — ⚠ 비번 메모 파일 유실, Metabase에서 재설정 필요.)
- ⚠ 운영 전: 메타DB H2→PG 이전 권장(기획서 §9)·Sample Database 제거 가능·계정 정책(공용 vs 개인).

## 4. 외부 노출 — `https://his.example.org/erp/bi/` (ERP 세션 SSO·재로그인 없음·사용자별 Editor)
**별도 Grafana 로그인 없이** ERP **경영 역할** 사용자만 접근. 구조: nginx가 `/erp/bi/` 요청마다 **auth_request로
ERP 세션을 `/auth/bi-check`에 검증** → **BI 역할(ADMIN·DIRECTOR·MANAGER·FINANCE_MANAGER)만 200**, 그 외(STAFF·
IT/물류관리자·WORKSTATION)는 **403 → `/erp/`로 리다이렉트**(BI 차단). 200이면 응답헤더 `X-Erp-User/X-Erp-Role`을
`auth_request_set`으로 캡처 → `X-WEBAUTH-USER/ROLE`로 Grafana **auth.proxy**에 주입. 역할: ADMIN→Admin·
MANAGER/DIRECTOR/FINANCE_MANAGER→**Editor**·(그 외는 게이트에서 이미 차단). ⚠**보안(재검토)**: 과거 `/auth/me`
(전 역할 200)를 auth_request로 써서 인증만 되면 누구나 Grafana Viewer 접근하던 공백을 `/auth/bi-check`로 닫음
— Metabase(`/integrations/bi/metabase-login`)의 역할 게이트와 대칭. BI를 `/erp/bi/` 하위로 둔 이유 = ERP 쿠키 Path=`/erp/`.

### (1) Grafana (ERP 측 — 무암호): auth.proxy + /erp/bi/ 서브패스
compose 반영됨: `GF_AUTH_PROXY_ENABLED=true`·`HEADER_NAME=X-WEBAUTH-USER`·`HEADERS=Role:X-WEBAUTH-ROLE`·
`AUTO_SIGN_UP=true`·`ANONYMOUS_ENABLED=false`·`GF_SERVER_ROOT_URL=…/erp/bi/`·`SERVE_FROM_SUB_PATH=true`·
홈=erp-executive. admin은 `/erp/bi/login`로 관리 로그인(fallback). 사용자 식별=ERP 이메일.
```sh
cd ~/his-backup/weruby/hospital-erp/infra
docker compose -f docker-compose.bi.yml --env-file .env.bi up -d grafana
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3011/erp/bi/api/health   # 200
```

### (2) HIS nginx 라우트 (슈퍼유저) — `/erp/bi/` + auth_request 게이트
`location = /erp/ {` 앞(같은 server{})에 추가:
```nginx
    # ERP 경영 BI (Grafana) — ERP 세션 인증 후 접근(별도 로그인 없음·익명 Viewer + auth_request 게이트)
    location = /erp/bi/_auth {
        internal;
        proxy_pass http://127.0.0.1:8001/api/v1/auth/bi-check;   # ★BI 역할만 200(비BI 403→아래 redirect)
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location /erp/bi/ {
        auth_request /erp/bi/_auth;            # ERP 미인증 → 401 → 아래 redirect
        auth_request_set $erp_user $upstream_http_x_erp_user;   # /auth/me 응답헤더 캡처
        auth_request_set $erp_role $upstream_http_x_erp_role;
        error_page 401 403 = @erp_bi_sso;
        proxy_pass http://127.0.0.1:3011/erp/bi/;
        proxy_set_header X-WEBAUTH-USER $erp_user;   # Grafana auth.proxy 사용자/역할 주입
        proxy_set_header X-WEBAUTH-ROLE $erp_role;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location @erp_bi_sso { return 302 /erp/; }
```
안전 적용(백업→test→reload):
```sh
sudo cp /etc/nginx/sites-enabled/hospital-run /etc/nginx/backups/hospital-run.$(date +%s)
sudo nano /etc/nginx/sites-enabled/hospital-run    # 위 블록 추가(/erp/ 블록 앞)
sudo nginx -t && sudo systemctl reload nginx
```
검증: ERP 로그인 상태로 `https://his.example.org/erp/bi/` → **로그인 없이** 대시보드 / 비로그인은 `/erp/`로 이동.
롤백: 백업 복원 후 `sudo nginx -t && sudo systemctl reload nginx`.

> ⚠ HIS nginx=병원 운영 — 백업+`nginx -t` 통과 후 reload. 한계: 익명 Viewer라 Grafana 내 사용자 식별 없음
> (공유 열람). 사용자별 식별/편집권한이 필요하면 GF_AUTH_PROXY(X-WEBAUTH-USER) 매핑으로 격상(후속).

## 5. 안전·자원
- **PII 없음**: reporting 마트만 — 환자/개인 식별자 무접근(erp_bi 권한 경계 검증됨).
- 자원 캡: Grafana 256M·Metabase 1.28G(busy 박스). 미사용 시 `down`으로 회수.
- 부하: erp_bi CONNECTION LIMIT 10 + 도구 maxOpenConns 5. 읽기 증가 시 읽기 복제본(기획서 §9).

## 5.5 알림 (Unified Alerting — 룰 프로비저닝 LIVE·이메일은 SMTP 대기)
`provisioning/alerting/erp-alerts.yml` — 룰 3종(6h 평가, 마트 야간 갱신 주기 맞춤):
- **미검토 감사·이상탐지 존재**: `mart_audit_flags_monthly.open_total > 0`(최근월)
- **자금 예측 최저잔액 5억 미만**: `mart_cash_forecast` 최신 스냅샷 `min(balance) < 5억`
- **부채비율 200% 초과**: `mart_exec_kpi_monthly.debt_ratio_pct > 200`(최근월)

상태는 Grafana Alerting UI(/erp/bi/alerting)에서 확인. ⚠ **이메일 발송은 미동작** — prod에
SMTP 미설정(ERP core smtp_host도 빈값). 발송하려면: ① SMTP 서버 결정 → ② compose에
`GF_SMTP_ENABLED=true`·`GF_SMTP_HOST`·`GF_SMTP_USER/PASSWORD`(.env.bi)·`GF_SMTP_FROM_ADDRESS`
→ ③ contact point(수신 이메일)·notification policy 연결. 임계 변경은 yml 수정 후 재기동(프로비저닝 룰은 UI 수정 불가).

## 6. 종료·회수
```sh
docker compose -f docker-compose.bi.yml down            # 컨테이너만(볼륨 보존)
docker compose -f docker-compose.bi.yml down -v         # 볼륨까지(설정·메타 삭제)
```

## 주의
- ⚠ 시크릿(`.env.bi`·core `.env`)은 600·미커밋. Grafana datasource 비번은 env 주입(파일 미커밋).
- 마트 갱신은 worker-batch 야간 잡(03:50). 즉시 필요 시 core에서 `refresh_*` 수동 호출(런북 1 참고).
