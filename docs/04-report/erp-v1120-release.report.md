# ERP v1.12.0 릴리스 — BI 완결(마트 11종·시각화·셀프서비스) + AI 폴러 안정화 + 신규코드 전체검토

> **Summary**: v1.11.0(2026-07-03) 이후 **PR #546~#552 · 마이그 0150~0151**. 4개 축.
> ① **BI 데이터/시각화 완결** — reporting 마트 **11종**(자금예측·연간재무비율 추가)·Grafana 신규 패널·
> 진료과 템플릿 변수·YoY·**알림 룰 3종**·Metabase 셀프서비스(컬렉션·모델 11종·Metrics·Segments).
> ② **AI 색인 폴러 안정화** — 22일 무한 재폴링 사고(HIS 트래픽 48%) 근본 종결: 종결 동의어 수용·
> **latest/jobs 파싱 복원**·지수 백오프·72h 컷오프. stuck 109건 전건 종결(DONE 100·DLQ 9).
> ③ **신규코드 전체검토(4갈래)** — BI 통합·폴러 신규 표면 보안·성능·정합·정직성. HIGH 2건(브리지
> 인가·폴러 예외 컷오프) 포함 Tier1~3 全 처리.
> ④ **정직성 정비** — 문서·도크스트링·example을 실제 동작과 일치(README·릴리스리포트·런북·config).
>
> **테스트**: 937 → **949**(+skip 2) | **마이그레이션**: 0149 → **0151** | **화면**: 66 | 11 모듈 ·
> reporting 마트 11종 · README **v1.12.0** · 전 PR CI 그린 · HIS 동거 무중단 배포 · BI 2종 LIVE.

---

## 1. BI 데이터/시각화 완결

### 1.1 reporting 마트 잔여 2종 — #546 (마이그 0150)
`mart_cash_forecast`(자금수지 예측 6개월·due_on 버킷·당월 스냅샷 전용)·`mart_ratios_annual`(연간
재무비율 6종+basis). 마트 **9→11종**으로 §0 완결. 야간 잡 연결(예측 당월·비율 연 단위).

### 1.2 Grafana 시각화 A트랙 — #547
FinOps에 자금예측 barchart·재무비율 추세 timeseries(8→10패널). 경영진 대시보드에 **$dept 진료과
템플릿 변수**(multi·All)·**월별 수익 YoY**(당년 vs 전년)·진료과별 손익 추세(6→8패널). **알림 룰 3종**
(Unified Alerting 프로비저닝·6h): 미검토 감사>0·자금 예측 최저잔액<5억·부채비율>200%. ⚠ 이메일 발송은
SMTP 미설정으로 미동작(룰 상태·UI만) — SMTP 결정 후 연결(런북 §5.5).

### 1.3 Metabase 셀프서비스 심화 B트랙 — #552
`infra/bi/metabase/provision.py`(멱등·메타DB는 git 밖이라 스크립트가 SoT): 컬렉션 3종(경영 BI·재무·운영·
감사·리스크)·**모델 11종**(마트별 한글 컬럼 alias·비개발자 질문빌더용)·**Metrics 4종**(월평균 수익/순이익·
평균 이직률·총 미사용검진권·v0.51 MBQL)·**Segments 3종**(적자 진료과·보고부문·미검토 감사월)·큐레이션 질문 재배치.

## 2. AI 색인 폴러 안정화 — #548·#549

**사고(HIS 진단 2026-07-03)**: `erp-notice-worker`가 완료된 색인 잡 50건을 22일간 30초 루프로 재폴링
= HIS `/ollama/api/notice/index-status` 전체 트래픽 48%. **원인 2중**: ① 스펙 `done`만 종결 인정(서버는
`completed`) ② 응답 파싱이 최상위 `status`만 봄(상태는 `latest`/`jobs[].status`) → 항상 "pending".
**수정**: 종결 동의어 수용(정규화)·`parse_index_status`(latest→status→jobs)·지수 백오프(마이그 0151
`poll_count`/`next_poll_at`)·72h 미종결 컷오프(DLQ). **복구**: stuck 109건 전건 종결(DONE 100·DLQ 9)·
SENT 0으로 폴링 트래픽 소멸. HIS측 max_requests 상향 불요 회신.

## 3. 신규코드 전체검토 (4갈래) — #550·#551

v1.10.0 이후 BI 통합·폴러 신규 표면을 보안·성능·정합·정직성 4갈래 병렬 검토 후 Tier triage.
- **[HIGH 보안]** Metabase 브리지 인가 게이트 — 인증만 확인(WORKSTATION 등 최말단 통과)하고 공용 세션
  발급하던 것 → 경영 역할(ADMIN/MANAGER/DIRECTOR)만 허용(403). 라이브 검증 WORKSTATION→403.
- **[HIGH 정합]** 폴러 예외 경로 72h 컷오프 — 서버 다운/404에도 age>72h면 DLQ("무한루프 불가" 보장 대칭화).
- **[MED]** Grafana `GF_AUTH_PROXY_WHITELIST`(헤더 스푸핑 방어심층)·`refresh_inventory` 당월만(스냅샷
  왜곡 제거)·`date.today()`→Asia/Seoul(월경계)·마트 잡 세이브포인트 격리(폭발반경)·poll_sent HTTP를
  트랜잭션 밖으로(idle-in-tx 제거).
- **Tier3 정직성**: README 지표·릴리스리포트 백필 서술·도크스트링·런북·example·죽은 테스트 모킹 정정.

## 4. 검증·배포
- 테스트 **949 통과**(+skip 2) · import-linter 7 kept · ruff 클린.
- 마이그 0150~0151 무손실 · HIS 동거 서버(198.51.100.73:8001) **무중단 배포** · alembic head 0151.
- **BI 2종 LIVE**: Grafana(경영 BI·/erp/bi/·알림 룰·진료과 변수) + Metabase(셀프서비스·/erp/mb/·모델·
  Metrics·Segments). 브리지 인가 라이브 검증(WORKSTATION 403·경영역할 302)·auth.proxy WHITELIST 정상.

## 잔여(백로그)
- **SMTP 선행**: Grafana 이메일 알림·Metabase Subscriptions/Alerts.
- **인박스 웹훅 논스/jti**: 재생창 방어(활성화 전·HIS 발신 서명 배포와 함께).
- **상태기반 마트 소급 오염**(aging/voucher/exec_kpi 잔액): 월말 실잔액 재현은 서비스 레벨 수정 필요(도크스트링 경고).
- Metabase per-user 감사(Enterprise SSO)·Sample DB 잔재 정리.
