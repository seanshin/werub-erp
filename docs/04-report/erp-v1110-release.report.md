# ERP v1.11.0 릴리스 — 오픈소스 BI 도구 통합(경영 BI·셀프서비스 BI) + reporting 마트

> **Summary**: v1.10.0(2026-06-29) 이후 **PR #519~#544 · 마이그 0145~0149(5건)**. 4개 축.
> ① **오픈소스 BI 도구 통합** — 운영 테이블 직결 대신 **PII 없는 reporting 마트**에만 연결하는 BI 계층.
> **경영 BI(Grafana)** = ERP 세션 SSO(재로그인 없음·nginx auth_request)·사용자별 Editor(auth.proxy)·
> 서브패스(/erp/bi/)·경영진/FinOps 대시보드. **셀프서비스 BI(Metabase OSS)** = 별도 추가(/erp/mb/)·
> 자동 로그인 브리지(ERP 인증 시 세션 발급)·서브패스 빈화면 근본해결(nginx 프리픽스 스트립).
> ② **reporting 마트 9종** — analytics·finance·scm 집계 메서드 재사용 마트화(경영KPI·진료과손익·감사·
> 자금수지·채권채무 Aging·기능별 손익·검진권·진료과 이직·재고). 읽기전용 롤 `erp_bi`(reporting 한정).
> ③ **/erp/analytics 표시 개선** — 박스·표 통화/수치 우측정렬·천단위 콤마·Benford 소표본 가드.
> ④ **보안·외부 트랙** — 인박스 웹훅 본문 HMAC+timestamp 검증(S-1/S-2 동형·OFF 기본)·e-sign relay 합동 E2E·
> 가정 수집기 폐기·공유 PG 점검창 제안.
>
> **테스트**: 920 → **937** | **마이그레이션**: 0144 → **0149** | **화면**: 65 → **66** | 11 모듈 ·
> README **v1.11.0** · 전 PR CI 그린 · HIS 동거 무중단 배포 · BI 도구 prod 기동.

---

## 1. 오픈소스 BI 도구 통합 (이번 릴리스 핵심)

### 1.1 BI 데이터 계층 — reporting 마트 + 읽기전용 롤 — #521·#522·#523·#525·#542·#543
운영 테이블 직결 금지 원칙으로, **PII 없는 집계 마트**(reporting 스키마)에만 BI를 연결.
- **마트 9종**: 경영KPI(`mart_exec_kpi_monthly`)·진료과손익(`mart_dept_pl_monthly`)·감사이상탐지
  (`mart_audit_flags_monthly`)·자금수지(`mart_cashflow_monthly`)·채권채무 Aging(`mart_aging_monthly`)·
  기능별 손익(`mart_income_function_monthly`)·검진권(`mart_voucher_monthly`)·진료과 이직
  (`mart_hr_turnover_monthly`)·재고(`mart_inventory_monthly`). 마이그 0145~0149.
- **집계 로직 이원화 회피**: 각 마트는 기존 analytics/finance/scm 서비스 메서드를 **그대로 호출**해 평탄
  컬럼으로 매핑(`analytics/marts.py` refresh_*). worker-batch 야간 잡이 당월·전월 갱신(스냅샷성 마트는
  당월만). 과거 추세는 **배포 시 refresh_* 수동 1회 백필**(2025-01~·야간 잡 기능 아님).
- **읽기전용 롤 `erp_bi`**: reporting 스키마만·connection limit 10. 운영 테이블 접근 불가.

### 1.2 경영 BI (Grafana) — 재로그인 없는 SSO·사용자별 권한·서브패스 — #524·#526·#528·#530·#532·#533
- **ERP 세션 SSO**: nginx `auth_request`가 `/auth/me`를 재사용해 게이트, **재로그인 없이** /erp/bi/ 진입.
- **사용자별 Editor**: `/auth/me`가 X-Erp-User/X-Erp-Role 헤더 반환 → Grafana `auth.proxy`로 per-user
  매핑(ADMIN→Admin·MANAGER/DIRECTOR→Editor·그 외 Viewer). 그래프 편집/저장 권한 분리.
- **서브패스**: GF_SERVER_ROOT_URL + SERVE_FROM_SUB_PATH로 /erp/bi/ 하위 정상 동작.
- **대시보드**: 경영진(executive) + FinOps(자금수지·손익·원가·채권채무·Aging·검진권·이직·재고) 프로비저닝 JSON.

### 1.3 셀프서비스 BI (Metabase OSS) — 별도 추가·자동 로그인·서브패스 근본해결 — #534·#538·#539·#540
- **별도 기능 추가**(Grafana 유지): 비개발자 셀프 탐색용 Metabase OSS를 /erp/mb/로 추가.
- **자동 로그인 브리지**: OSS는 헤더 SSO 미지원 → ERP 인증 사용자가 링크 클릭 시 브리지 엔드포인트가
  공유 계정으로 Metabase 세션을 발급(`metabase.SESSION` 쿠키 주입)·세션 없으면 nginx가 브리지로 리다이렉트.
- **빈 화면 근본해결**: Metabase 프론트가 base href를 (백엔드 수신 URI − 브라우저 경로)로 계산 →
  nginx `proxy_pass`에 **트레일링 슬래시**로 /erp/mb 프리픽스를 스트립해야 자산 경로가 맞음(404 해소).

## 2. /erp/analytics 표시 개선 — #531·#532·#544
- **우측정렬**: KPI 박스 값·표 숫자 컬럼 통화/수치 우측정렬(`tabular-nums`)·`_NUM` 숫자감지에 만원/억원/천원
  접미사 추가(날짜·식별자는 제외 유지).
- **천단위 콤마**: 경영 BI 표 금액 locale 표시.
- **Benford 소표본 가드**: 표본<300이면 판정 보류("표본부족(N/A)") — 경영 BI 오해 방지.

## 3. 보안·외부 트랙 — #519·#520
- **인박스 웹훅 본문 HMAC + timestamp**: 발신측 재무 콜백(S-2)과 동형 검증을 인박스 수신에도 추가(재생/위변조
  방어)·OFF 기본(키 배포 후 활성). HIS 측 발신 서명 요청 동봉.
- **e-sign #6 후속**: 다자 봉투(STAFF+PORTAL 혼합) relay 운영 검증 스크립트·**가정 수집기(his.py) 폐기**·
  공유 PG 4자 점검창 제안(코딩 없음·합의 대기).

---

## 검증·배포
- 테스트 **937 통과**(+skip 2) · import-linter 7 kept(모듈 경계 0 broken) · ruff 클린.
- 마이그 0145~0149 무손실(reporting 스키마·마트 9종) · HIS 동거 서버(198.51.100.73:8001) **무중단 배포** ·
  alembic head 0149 · health OK.
- **BI 도구 기동**: Grafana(경영 BI, /erp/bi/) + Metabase OSS(셀프서비스, /erp/mb/) prod 기동·SSO/자동로그인 검증.
- BI는 **reporting 마트만** 읽음(읽기전용 롤 erp_bi). 운영 테이블 직결 없음·PII 없음 원칙 유지.

## 잔여(의도적 미처리·백로그)
- **마트 확장 2종**: `mart_cash_forecast`(자금예측)·`mart_ratios_annual`(재무비율 추세) — §0 패턴 반복(저위험).
- **Grafana**: 템플릿 변수(진료과 드롭다운)·Unified Alerting(임계 알림)·YoY 오버레이. 스케줄 PDF는 Enterprise.
- **Metabase**: Models/Metrics/Segments·Subscriptions/Alerts(SMTP 선행)·per-user 식별(OSS 공용계정 한계).
- 상세 후보: `docs/01-plan/features/bi-enhancements.plan.md`(각 10종 구현 가능 점검).
