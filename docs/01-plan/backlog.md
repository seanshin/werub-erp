# ERP 백로그 (실 운영 전제 — 데이터/외부 확보 시 착수)

> 인프라·연동은 완성, 실데이터 확보 시점에 진행할 항목.

## AI 사전심사 P1 — 실 운영 게이트
- **G1 통보서 파서(MEDRLT)** 🔴 — 실 심사결과통보서 샘플 확보 후 착수. 산출물 = 실삭감 라벨
  (`{claim_seq, code, service_date, deducted, adjust_code, deduct_amount, context}`) → weve P0.5 섀도
  캘리브레이션 → 위험밴드 임계 보정 → **P1 노출**. (weve diagnose는 이미 가동, 정확도 합의만 미완)
- **실 고시 본문 추가 적재** 🟡 — 보건복지부/심평원 고시 파일 확보 시 `scripts/import_notices.py`(CSV)
  또는 PDF 추출(`pdftotext -layout`)로 적재 → immediate 자동 색인. (현재 실고시 2건: 125·118호)

## HIS 연동 잔여
- **procurement.cssd.po.approved consumer** — HIS payload 스키마 수신 후 구현(마지막 미구현 consumer).
- **비급여 가격(non-covered) push** — ERP에 비급여 고지 가격표 확보 시.
- **material-costs push 실데이터** — 발주(PO) 이력 축적 시 실 매입단가 push(파이프라인은 구축됨).

## 경영진 분석 BI — 오픈소스 BI 도구 통합 (기획)
- 기획서 [features/bi-tool-integration.plan.md](features/bi-tool-integration.plan.md)(2026-06-30) — 현 analytics
  API+SVG에 BI 도구(Grafana 재사용/Metabase) 추가. **선행 필수 = PII-safe·RBAC 내장 `reporting` 마트 +
  읽기전용 롤(커넥션 캡)**(작업 80%). 의사결정 게이트: 스코프(대시보드 vs 셀프서비스)·PII 정책·인증·복제본 여부.

## 품질·성능 점진 개선 (2026-06-16 전체검토 — 별도 사이클)

> 2026-06-16 전체 시스템 검토(보안·품질·갭·구조)에서 보안 4건(PR #279)·재무 동시성
> 3건(PR #280)·정직성(PR #281)·백엔드 quick-win 4건(N+1·PII 풀스캔·UoW savepoint·
> ValueError→Validationable, PR #282)은 즉시 해소. 아래는 범위가 넓어 전용 사이클 대상.

- ✅ **scope 게이트 통일** (PR #283) — 7개 라우터(finance/regulatory/analytics/costing/tax/admin/
  export)의 `resolve_scope`+수동 NONE 중복을 `require_scope` 단일 게이트로 위임 완료.
- ✅ **프론트 포맷 공통화** (PR #284) — `lib/format.ts`(won/wonOrDash/dt) 추출, ~18개 페이지 교체.
- ✅ **window.prompt → 검증 다이얼로그** (PR #287·#288) — `useInputDialog`(숫자/텍스트/날짜·min/max/
  required·커스텀 validate) 신설, 금액·사유·날짜·JSON 등 전량 교체. **앱 전역 prompt 0건**.
- ✅ **목록 페이지네이션** (PR #285·#286) — 약품코드 매핑·세금계산서·채권/채무(AR·AP) page/size+
  total+이전/다음. aging는 미정산 잔액 합산이라 페이지네이션 아닌 SQL 집계 대상(단 date 버킷은
  PG/sqlite 이식성 주의 — Python 유지 가능).
- ✅ **API 타입 집약 조사** (PR #289) — 동명 인터페이스 전수조사: 대부분 shape 상이한 페이지별
  프로젝션(정당)이라 강제 공유 부적절. 진짜 중복 `Emp`만 `EmployeeRef`로 집약. 추가 대상 없음(종결).
- ✅ **프론트 동적 행 안정 key** — 편집형 동적 폼(전표 수동작성 `finance/page.tsx`)의 행 `key={i}`를
  불변 `id`(모듈 카운터 `blankLine`)로 교체 — 중간 행 삭제 시 입력값·포커스 밀림 제거. 나머지
  index key(차트 Cell·정적 에러리스트·서버 조회 테이블)는 행별 로컬상태 없어 무해 → 유지.
- **worklist 롤업 SQL 집계** 🟡(보류) — `_refresh_rollup` risk_amount가 "코드별 마지막 금액 dedup"
  (행순서 의존)이라 SQL 정확 변환이 까다롭고, 케이스당 finding N이 작아 이득 미미. 의미변경
  위험 > 이득 — 모델 정리(코드별 1행 제약) 시 함께 처리.
- ✅ **동시성 통합 테스트(PG)** (PR #290) — settlement collect 행잠금(PR #280)을 실 PostgreSQL
  asyncio.gather로 검증(lost-update 없음·과수금 차단). `RUN_PG_INTEGRATION` 게이트, CI 마이그레이션
  직후 ephemeral PG 전용 스텝(2 passed). 잔여: budget.amend·pay도 같은 틀로 확장 가능(저우선).

## 메모
- 위 항목은 코드/인프라가 아닌 **데이터·외부 회신** 대기. 확보 시 즉시 착수 가능.
