# Zero Script QA 시나리오 목록

> **방법론**: 테스트 스크립트 없이, 수동 트리거 후 **구조화 JSON 로그**(`erp.logging.JsonFormatter`)를 실시간 관측해 검증한다.
> 각 로그 라인은 `{ts, level, request_id, module, event, ctx{…}}` 형태. `request_id`로 한 요청의 전 구간을 추적한다.
>
> **로그 관측 기본기**
> - 코로케이션 운영: `docker compose -f infra/docker-compose.coloc.yml logs -f core` (또는 `prod.yml`)
> - 도메인별 필터: `… | grep '"module":"erp.ai"'`, 특정 요청: `… | grep '<request_id>'`
> - 합격 = "기대 event가 기대 `ctx`와 함께 관측됨" + "ERROR/스택(`exc_type`) 부재". 불합격 = 기대 event 미관측 또는 예기치 않은 ERROR.
>
> 범위: core 10개 도메인(analytics·claims·common·costing·finance·hr·patient·regulatory·scm·tax) + 통합(HIS 웹훅/인박스·AI 디스패치) + web 60화면.

---

## 1. 인증·세션 (common / SSO / 2FA)

### S1.1 SSO 로그인 성공
- **트리거**: HIS SSO 진입 → ERP 랜딩 → 대시보드.
- **관측**: `module=erp.common.sso_service`, `event="SSO 로그인"` 류, `ctx.email`=마스킹(`a***@…`), `ctx.role` 존재.
- **합격**: 평문 이메일 미노출(마스킹 확인), 토큰 발급 후 `/api/health` 외 보호 EP 200.

### S1.2 PIN+패스키 2FA
- **트리거**: `/erp/login` PIN 입력 → 패스키 인증 → 토큰 발급.
- **관측**: 2FA 1단계(PIN 검증) 통과 로그, 실패 시 `level=WARNING`.
- **합격**: PIN 불일치 시 토큰 미발급(401), 계정 잠금 임계 초과 시 423(`AccountLockedError`).

### S1.3 CSRF 방어 (상태변경)
- **트리거**: 쿠키 인증 상태에서 잘못된 Origin/누락 토큰으로 POST.
- **합격**: double-submit 토큰 불일치 → 거부, 정상 헤더 시 통과.

---

## 2. 사전심사·근거검색 (claims / regulatory) — *최근 작업 영역*

### S2.1 근거검색 다수결 정상 (#320)
- **트리거**: 사전심사 명세 라인 "근거 검색" 클릭.
- **관측**: `module=erp.ai`. weve 3회 호출. 응답에 `samples`/`attempted`/`risk_votes`.
- **합격**: 동일 명세 재조회 시 위험도 판정 안정(다수결). 화면 배지 "다수결 N/M회 (HIGH·LOW…)" 표시.

### S2.2 근거검색 부분 실패 → degraded (#320 C1)
- **트리거**: weve 일부 표본 실패 유도(네트워크 순단/타임아웃 또는 키 일시 오류).
- **관측**: `erp.ai` `event="ai-diagnose weve 호출 실패 code=… : <ExcType>"` (타입명만, 본문/URL 미노출).
- **합격**: 응답 `degraded:true`, 화면 "⚠ 표본부족 N/M회". 비가시화되지 않음.

### S2.3 근거검색 전체 실패 / 타임아웃 (#320 C2)
- **트리거**: weve 전면 장애 또는 전체 타임아웃.
- **관측**: `erp.ai` WARNING(전체 실패 또는 "전체 타임아웃").
- **합격**: 응답 `available:false`(note=일시 오류/응답 지연), 500 미발생, 스택(`exc_type`) 응답 본문 미노출.

### S2.4 ai-diagnose rate-limit (#322 M2)
- **트리거**: 동일 사용자가 한도(`weve_diagnose_rate_max`, 기본 20/60s) 초과 연타.
- **관측**: 한도 내 정상, 초과 시 **429**(`ERP-1006`, `ctx`/응답 `retry_after`).
- **합격**: 외부 weve 호출이 한도 초과분에서 **발생하지 않음**(증폭 차단). 윈도우 경과 후 재허용.

### S2.5 비식별 게이트 차단
- **트리거**: description/context에 PII 추정 문자열 포함해 ai-diagnose 호출.
- **합격**: 422(`ValidationError` "개인식별정보 추정") — 외부 전송 전 차단. weve 호출 로그 부재.

### S2.6 근거 부족 → 판단보류
- **트리거**: 근거 고시가 과반에서 없는 코드 조회.
- **합격**: 응답 `held:true`, 화면 "판단보류(근거부족)" + 로컬 룰만 적용.

---

## 3. HIS 연동·인박스 (integrations)

### S3.1 HIS 웹훅 → 실시간 사전심사 sync
- **트리거**: HIS `claims.*` 웹훅 수신(또는 5분 폴링 안전망).
- **관측**: `module=erp.integrations.inbound`, 표적 사전심사 재실행 로그.
- **합격**: 해당 케이스만 갱신(전건 스캔 아님), `request_id` 일관.

### S3.2 인박스 적용 실패 → 재처리/DLQ
- **트리거**: 잘못된 payload 이벤트 인입.
- **관측**: `erp.integrations.inbound` `event="inbound 적용 실패"`, `ctx{id, evt}`.
- **합격**: FAILED 상태 보존 → 재처리 가능, 임계 초과 시 DEAD, 정상 이벤트는 `SKIP LOCKED`로 병행 처리.

### S3.3 HIS 수납 소비
- **트리거**: HIS 수납 이벤트 인입.
- **관측**: `module=erp.patient.ingest`, `event="HIS 수납 소비"`, `ctx{applied, failed}`.
- **합격**: `applied`>0, `failed`=0(정상 케이스).

### S3.4 직원-계정 email 연결 (staff-sync)
- **트리거**: HIS 직원 이벤트 인입.
- **관측**: `erp.integrations.inbound` `event="직원-계정 email 연결"`.
- **합격**: email 매칭 성공, 미매칭 시 명시 로그.

---

## 4. 재무·결산 (finance / tax / costing)

### S4.1 전표 역분개
- **트리거**: `POST /finance/vouchers/{id}/reverse`.
- **관측**: `module=erp.audit` 머니변경 감사(`_money_audit`).
- **합격**: 역분개 1회 멱등(중복 차단), 차변/대변 합 0 검증.

### S4.2 월마감 일괄 (감가·충당·리스)
- **트리거**: 월마감 잡 실행.
- **합격**: 멱등(재실행 시 중복 전표 미생성), 기간 잠금 후 소급 변경 차단.

### S4.3 전자세금계산서 대사
- **트리거**: 홈택스 CSV 업로드 → 미지급금 대사.
- **합격**: 승인번호 멱등, 금액 불일치 시 미대사로 분리, 행별 오류 격리(전체 실패 아님).

### S4.4 원가 ABC 스냅샷·진료과 손익 (costing)
- **트리거**: `POST /costing/snapshot` → `GET /department-pl`.
- **합격**: 배부 정합 `reconciled:true`(부서 합=풀 합), 손익=수익−배부원가−삭감손실.

---

## 5. 인사·급여 (hr)

### S5.1 근태→급여 자동산정 + OT
- **트리거**: 근태/듀티 입력 후 급여 일괄 생성.
- **합격**: 수당단가 마스터(`hr.pay_rate_settings`) 주입 확인, OT 가산 반영.

### S5.2 퇴직 정산 멱등
- **트리거**: 퇴사 처리 → 정산(연차수당+퇴직금+충당상계 전표).
- **합격**: 1회 멱등, 발령 연동 SoT 가드 동작.

### S5.3 ESS 연동 (X-Integration-Key)
- **트리거**: 급여명세/연차/원천징수 API 외부 조회.
- **합격**: 키 없으면 거부, 로그 PII 마스킹(`mask_email`/`mask_id`).

---

## 6. 자재 (scm)

### S6.1 재고 워크스테이션 입고·라벨
- **트리거**: 스캐너 입고 → 바코드 라벨 출력.
- **합격**: 입고 반영, 부서 경계(IDOR) 차단 확인.

### S6.2 FIFO 재고자산 평가·실사
- **트리거**: 재고 실사 조정.
- **합격**: 감모손실/조정이익 전표 정확, 회전율 집계.

---

## 7. 권한·경계 (RBAC / IDOR) — 횡단

### S7.1 부서 경계 (행수준 scope)
- **트리거**: 타 부서 자원(budget/patient/absence) 조회·변경.
- **합격**: scope=NONE → 403, 본인 부서만 노출.

### S7.2 미등록 정책 / 이중 prefix
- **트리거**: 라우트 스모크(전 EP 인증 검증).
- **합격**: 404(이중 prefix)·403(RBAC 미등록) 결함 부재. `tests/test_route_smoke.py` 대응.

---

## 8. 관측성·감사 (audit) — 횡단

### S8.1 access_log 스트림 적재
- **트리거**: 임의 보호 EP 호출.
- **관측**: `module=erp.audit`. 정상 적재, 실패 시 `event="access_log stream 적재 실패 — 동기 폴백"`.
- **합격**: 폴백 동작(유실 없음), `request_id` 전 구간 일관.

### S8.2 PII 로그 마스킹
- **트리거**: 이메일/주민번호 포함 경로.
- **합격**: 로그 `ctx`에 평문 PII 부재(`a***@…`, ID 마스킹).

---

## 부록 — 실행 체크리스트

1. 환경 기동: `docker compose -f infra/docker-compose.coloc.yml up -d`
2. 로그 스트림 열기: `docker compose -f infra/docker-compose.coloc.yml logs -f core | grep -E '"level":"(WARNING|ERROR)"'` (이상징후 우선 감시)
3. 시나리오별 트리거 수행 → 기대 `module`/`event`/`ctx` 관측 → 합격/불합격 기록
4. 불합격 시 `request_id`로 전 구간 추적 → 결함 티켓화
5. 헤드리스 토큰 필요 시: 컨테이너 `mktoken` 패턴(DB `token_version` 일치 — `0` 하드코딩은 401)

> 외부 의존 시나리오(weve 장애·HIS 인입)는 운영/스테이징에서 실데이터로 재확인. 합성 데이터는 파이프 검증만, 캘리브레이션은 실물 개시 후.
