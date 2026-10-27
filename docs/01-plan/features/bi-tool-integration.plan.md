# 경영진 분석 BI — 오픈소스 BI 도구 통합 기획서

> **요약**: 현 경영진 분석(`analytics/` 18개 EP + 의존성-0 SVG 차트)에 오픈소스 BI 도구를 더해
> **셀프서비스 ad-hoc 탐색·대시보드 빌더·스케줄 리포트**를 확보한다. 단, 이 배포(공유 PG·PII 컬럼암호화·
> 앱레이어 RBAC·busy 박스)에서 핵심 작업은 도구 "설치"가 아니라 **PII-safe·RBAC 내장 reporting 계층**
> 설계다(작업량의 ~80%). BI 도구는 그 위에 얹는 마지막 ~20%.
>
> **작성일**: 2026-06-30 · **버전**: v0.1(기획) · **상태**: 기획(의사결정 게이트 §10) · **우선순위**: Medium
> **연관**: `analytics/router.py`·`analytics/service.py`, design BI(#5), [[erp-perf-sql-tuning]](공유 PG),

---

## 1. 현황 (verified 2026-06-30)
- **분석 API**: `analytics/router.py` 18개 EP — `overview`·`executive`(8도메인 KPI)·`department-pl`(+lines)·
  `segments`·`trend`·`hr-turnover`·`voucher`·`sim-*`(재무 시뮬)·`audit/*`(benford·duplicates·abnormal).
  전부 `require_scope(user, "analytics.reports", "read")` **앱레이어 RBAC** 통과 후 Python 집계.
- **웹**: `/erp/analytics` 등 — 의존성-0 SVG(`charts.tsx`). 의도적 경량 선택(번들·리스크 최소).
- **reporting 계층 없음**: 마트·뷰·집계 테이블·읽기 복제본 전무 — 매 분석이 운영 테이블 위 런타임 집계.
- **PII 컬럼암호화**: `phone_enc`·`tax_id_enc`·계좌 등 AES-GCM 컬럼암호(`common/crypto.py`). DB 직접 조회 시 **암호문**.
- **인프라**: 공유 `docker-postgres-1`(max_conn 200, OpenPACS 누수로 슬롯고갈 이력) · busy 박스(15Gi, HIS PM2·
  OpenPACS·Jitsi·**Grafana/Prometheus 이미 가동**) · `erp_app`=비특권 롤.

## 2. 목표 / 비목표
**목표**
- 비개발 경영진/기획의 **셀프서비스 ad-hoc 탐색**(드릴다운·피벗·신규 차트, 개발 의존 제거).
- **대시보드 빌더 + 스케줄 리포트/알림**(정기 경영 리포트 자동 발송·임계 경보).
- 기존 analytics API·SVG는 **유지**(운영 화면 내 RBAC-safe 위젯) — BI 도구는 보완재.

**비목표**
- 운영 화면을 BI 도구로 대체하지 않음. 환자 PII·개인 보수 단위 조회를 BI로 노출하지 않음.
- 실시간(<1분) 분석 — 마트는 배치 갱신(시간/일) 전제.

## 3. 핵심 제약 (설계를 지배하는 4가지)
1. **PII 암호화** — BI가 erp DB 직접 연결 시 환자명·전화·딜러 세무가 암호문. 집계는 OK, PII 단위는 노출(복호 뷰)
   이거나 차단돼야 함. → **마트는 집계만·PII 제외**.
2. **RBAC 우회** — 부서 행단위 스코핑·IDOR 가드는 앱레이어. BI 직접 SQL은 우회 → 부서장이 전 부서 조회 가능.
   → **마트에 부서 스코핑을 사전집계로 구워넣음**(BI측 RLS 재구성보다 견고).
3. **공유 PG 커넥션 압박** — BI 풀을 그대로 얹으면 슬롯고갈 위험. → **전용 읽기전용 롤 + 하드 커넥션 캡**,
   이상적으론 **읽기 복제본**(또는 예정된 DB 서비스분리).
4. **Busy 박스** — Superset(Python+celery+redis+메타DB) 무겁고 충돌 위험. 단일 컨테이너 도구 선호.

## 4. 아키텍처
원천 테이블 직결 금지 — **PII-safe·RBAC 내장 reporting 계층**에만 BI 연결:
```
erp(운영 스키마들) ──[배치 갱신]──▶ reporting 스키마 (집계 마트/뷰, PII 제외·부서 사전집계)
                                            ▲ (읽기전용 롤 erp_bi, 커넥션 캡)
                                     BI 도구 (reporting만, SSO)
```
- **`reporting` 스키마** 신설(마이그) — 운영 스키마와 분리. `erp_bi` 롤은 `reporting`에만 SELECT, 운영 스키마 GRANT 없음.
- 갱신: worker-batch 야간 잡(기존 스케줄러 패턴) 또는 `REFRESH MATERIALIZED VIEW`. analytics 서비스 집계 로직 재사용.
- 커넥션 캡: `erp_bi`에 `ALTER ROLE erp_bi CONNECTION LIMIT 10` — 슬롯고갈 방어.

## 5. reporting 마트 (초안 — 기존 analytics 출력 기반)
| 마트 | 원천(analytics) | 입도 | PII |
|---|---|---|---|
| `mart_exec_kpi_monthly` | `executive`(8도메인) | 월 | 없음(집계) |
| `mart_dept_pl_monthly` | `department-pl` | 월×부서 | 없음 |
| `mart_segment_pl` | `segments`(진료과 손익) | 월×부문 | 없음 |
| `mart_finance_trend` | `trend` | 월 | 없음 |
| `mart_hr_turnover` | `hr-turnover` | 월×부서 | 없음(인원수만) |
| `mart_voucher_summary` | `voucher` | 월×계정 | 없음 |
| `mart_audit_flags` | `audit/benford·duplicates·abnormal` | 일 | 없음(플래그·건수) |
> 전부 **집계·코드/부서 단위** — 개인·환자 식별자 없음. PII가 필요한 분석은 의도적 범위외(운영 화면 RBAC 유지).

## 6. 도구 선택
| 도구 | 적합 | 박스부담 | 권장 |
|---|---|---|---|
| **Grafana**(이미 설치) | 경영 KPI/시계열 대시보드 | 0(재사용) | **대시보드 스코프 1순위** |
| **Metabase** | 비개발 셀프서비스 ad-hoc·SSO(JWT) | 중(단일 JVM) | **셀프서비스 니즈 1순위** |
| Superset | 고급 커스텀 | 고(celery+redis+메타DB) | 비권장(busy 박스) |

**결정 변수 = 경영진이 "ad-hoc 셀프서비스"를 원하나, "큐레이션 대시보드"면 충분한가**(§10-A).
- 대시보드면 충분 → **Grafana 재사용**(신규 서비스·운영부담 0, reporting 읽기전용 datasource).
- 셀프서비스가 진짜 니즈 → **Metabase**(reporting 복제본/스키마 + SSO).

## 7. 보안·거버넌스
- **PII 비노출**: 마트 집계 전용·원시 PII 제외 → BI는 새로운 PII 접근 경로가 되지 않음(의료기관 컴플라이언스).
- **읽기전용·최소권한**: `erp_bi`는 `reporting` SELECT만. 운영 스키마·`erp_app` 권한과 분리.
- **접근 감사**: BI 도구 자체 로그인 감사 + PG `log_connections`(reporting 롤). 누가 무엇을 봤는지 추적.
- **인증**: Grafana=기존 조직 인증/프록시, Metabase=JWT SSO(HIS SSO 또는 ERP JWT 연계 검토). 별도 계정 최소화.
- **망**: BI 컨테이너는 내부망(nginx 뒤). 외부 직노출 0(ERP 패턴 일관).

## 8. 단계안
- **P0 — reporting 계층(BI 무관, 선행 필수)**: `reporting` 스키마 마이그 + 마트 6~7종 + 야간 갱신 잡 +
  `erp_bi` 읽기전용 롤(커넥션 캡). analytics 집계 로직 재사용. **이 단계만으로도** 운영 화면/엑셀 리포트가 마트 활용.
  - ✅ **P0-a 골격 LIVE(2026-06-30, 마이그 0145)**: `reporting` 스키마 + `mart_exec_kpi_monthly`(경영진 KPI
    평탄 컬럼 + payload 스냅샷, 집계·PII 없음) · `analytics/marts.refresh_exec_kpi`(executive() 재사용·멱등 upsert) ·
    야간 잡 `job_refresh_bi_marts`(당월·전월, 03:50) · 읽기전용 롤 런북
    [bi-reporting-role.runbook.md](../../05-operations/bi-reporting-role.runbook.md). 테스트 3건·923 통과.
  - ✅ **P0-b 부서 드릴다운 LIVE(2026-06-30, 마이그 0146)**: `mart_dept_pl_monthly`(진료과별 수익·손익·자산·
    보고부문 — segment_report() 재사용·전건 delete-then-insert 멱등). 잡이 당월·전월 dept_pl도 갱신. 테스트 5건·925 통과.
    ⚠ 통찰: exec KPI 마트가 이미 **월별 톱라인 시계열**(매월 1행)이라 trend/turnover/voucher 스칼라는 중복 →
    BI는 exec 마트를 yyyymm 정렬로 추세 표시. 추가가치는 **부서 차원**이라 dept_pl을 우선 구현.
  - ✅ **P0-c 감사·이상탐지 마트 LIVE(2026-06-30, 마이그 0147)**: `mart_audit_flags_monthly`(Benford·중복전표·
    비정상분개 카운트 — audit_service.summary() 재사용·멱등 upsert). exec/dept 마트에 없는 차원(감사). 잡에 추가.
    테스트 7건·927 통과. (voucher by_line_type 등은 exec 검진권 집계와 중복이라 미구현.)
- **P1 — Grafana POC**: 기존 Grafana에 `erp_bi` PG datasource(reporting) + 경영 KPI 대시보드 1~2개. 신규 서비스 0.
  → 경영진 피드백으로 "대시보드면 충분 vs 셀프서비스 필요" 실측.
  - ✅ **P1 ERP측 LIVE+배포(2026-06-30)**: 마이그 0145·0146 prod 적용(alembic head 0146·reporting 마트 2종) ·
    `erp_bi` 읽기전용 롤 생성(reporting SELECT=t·finance/patient USAGE=f 검증·커넥션 캡 10·비번 prod 600 파일
    `~/hospital-erp/erp-bi-secret.txt`) · 마트 실데이터 적재 검증(2026-05 수익 6.89억·재직 251·부서 15세그먼트).
    야간 잡 03:50 자동 갱신.
  - ⬜ **P1 잔여 = 시각화(외부 게이트)**: prod Grafana는 **HIS·jitsi·openpacs 소유**(`his-grafana`, ERP 전용 없음)
    → his-grafana에 erp_bi datasource+대시보드 추가는 **HIS 협조 필요**, 또는 ERP 자체 Grafana 컨테이너(신규 서비스 결정).
  - ✅ **P1/P2 ERP 자체 BI IaC 구현(2026-06-30, PR#526)**: HIS Grafana 협조 불요 경로 — `infra/docker-compose.bi.yml`(opt-in)
    + Grafana 자동 프로비저닝(erp_bi→reporting datasource·**경영진 대시보드 6패널** 수익·재무비율·인력·감사·진료과) +
    Metabase(셀프서비스) 서비스. 비번 env(미커밋). 런북 [bi-tools-runbook.md](../../05-operations/bi-tools-runbook.md).
  - ✅ **P1 Grafana POC prod 기동·검증(2026-06-30)**: `erp-grafana`(256M) prod LIVE — health ok·datasource
    `ERP-reporting` 프로비저닝·**datasource health "Database Connection OK"**·실쿼리 200(mart_exec_kpi 2026-05/06 실데이터)·
    대시보드 로드. 접근=SSH 터널 `-L 3011:127.0.0.1:3011`(admin 비번 prod `.env.bi`). **P1 동작 입증**.
  - ⬜ **P2 Metabase**: IaC 동봉, **미기동**(JVM 무거움·최초 마법사 setup·셀프서비스 실수요 확정 후). 운영 전 메타DB H2→PG.
- **P2(조건부) — Metabase**: 셀프서비스가 확인되면 단일 컨테이너 배포 + SSO + reporting 연결. 복제본 검토.
  - ✅ **P2 IaC 동봉(2026-06-30)**: 위 `docker-compose.bi.yml`에 Metabase 서비스 포함(H2 메타DB POC·reporting 연결은
    최초 마법사). 셀프서비스 실수요 확정 시 기동(런북 §3). 운영 전 메타DB H2→PG·SSO 검토.
- **P3 — 스케줄 리포트/알림**: 정기 경영 리포트 발송·임계 경보(도구 내장 기능).

## 9. 운영·배포
- compose: BI 도구는 ERP `docker-compose.coloc.yml`와 **별개**(또는 Grafana 재사용 시 신규 0). 메모리 상한 명시(busy 박스).
- 커넥션: `erp_bi CONNECTION LIMIT 10` + 도구 풀 캡. 마트 갱신은 worker-batch 진료시간 외.
- **읽기 복제본 검토**: 셀프서비스(Metabase, 임의 쿼리) 도입 시 공유 PG 부하 격리를 위해 복제본 우선 — 예정된
  DB 서비스분리 백로그와 연계([[erp-prod-deploy]] DB 분리).

## 10. 의사결정 게이트 (선행 필요)
- **A. 스코프**: 큐레이션 대시보드(→Grafana) vs 비개발 셀프서비스 탐색(→Metabase). **도구·복제본 필요성 결정.**
- **B. PII 정책 확정**: "마트는 집계·PII 제외" 원칙 승인(권장). 개인/환자 단위가 BI에 필요하면 별도 거버넌스·감사 설계 필요.
- **C. 인증 연계**: BI 도구 로그인 = HIS SSO 연계 vs 별도 BI 계정(최소). 
- **D. 인프라**: 공유 PG 직결(캡) 수용 vs 읽기 복제본 선행(부하 격리·DB 분리 백로그 연계).

## 11. 리스크
- 공유 PG 부하(임의 쿼리) → 복제본/캡으로 격리. 미격리 시 슬롯고갈 재발 위험.
- PII 거버넌스 — 마트 집계원칙 미준수 시 새 PII 노출면. 설계 게이트(B)로 차단.
- 박스 자원 — Metabase/Superset 메모리. Grafana 재사용이 자원 0.
- 중복 유지비 — 마트가 analytics 집계와 이원화되지 않도록 **동일 집계 로직 재사용**(서비스 → 마트 갱신).

## 12. 권장
**P0(reporting 마트 + 읽기전용 롤)는 도구 선택과 무관하게 가치가 있고 선행 필수** — 먼저 착수 권장.
이후 **기존 Grafana로 P1 POC**(자원 0)로 경영진 셀프서비스 실수요를 실측한 뒤, 필요 시에만 Metabase(P2)로 확장.
Superset은 busy 박스 비권장.
