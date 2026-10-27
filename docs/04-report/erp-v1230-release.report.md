# ERP v1.22.0 → v1.23.0 릴리스 — 증분 코드 전체검토 + 지급 대사 강화 + 출력물 표기·기관 설정

> **Summary**: v1.21.1(2026-07-06) 이후 **PR #641~#647 · 3 릴리스 · 마이그 0160 → 0162 · 테스트 1069 → 1086**.
> ① **증분 코드 전체검토** — v1.16.1~v1.21.1 미검토 27 PR을 4갈래 병렬 심층 검토, 확증 결함 5건 보완
> (변동대가 true-up 델타 유실 MED 포함). ② **공단 지급 대사 강화** — ADMIN 전용 게이팅(직무분리)+과다지급
> 선수금(2141) 분리·상계. ③ **출력물·레포트 표기 전수 검토** — 증명서 "OO병원장" 견본 문구 근절.
> ④ **기관 설정 관리화면** — 발급기관 표기 SoT를 DB로 승격(/admin/org).
>
> **테스트**: 1069 → **1086**(+skip 2) | **마이그레이션**: 0160 → **0162** | 화면 67 → **68** ·
> README **v1.23.0** · 전 PR CI 그린 · **prod 2회 배포·검증 완료**(HIS 동거 무중단).

---

## 1. 증분 코드 전체검토 — v1.22.0 (#641)

v1.16.1~v1.21.1에서 도메인 재검토 없이 머지된 27 PR(심사청구 A~E·care_month·10원절사·마스터코드 EP)을
4갈래 병렬 심층 검토(지급대사/배치 · 재청구/이중소스/변동대가 · 라우터 RBAC/IDOR · 횡단 소패치).
각 갈래 refute-first 적대적 검증 후 확증 결함만 보완.

- **[MED] 변동대가 true-up 델타 유실**: `post()`의 source_ref가 월 고정(`var-consideration:{yyyymm}`)이라
  VoucherService (module,ref) 멱등에 걸려 같은 월 2번째 이후 증분이 전기 안 되면서 `posted:True` 거짓 반환
  → 환불부채(2155) 과소·수익(4110) 과대. **posting별 순번(vN) 유니크화**(#627 월고정키 멱등충돌 동형 —
  재발 패턴으로 등재).
- **[MED] 지급/반송 멱등키 공백 불일치**: dedup SELECT·voucher source_ref는 raw, 저장은 strip → 공백만
  다른 재전송이 uq IntegrityError. 진입점 1회 정규화.
- **[LOW]** 변동대가 as-of `submitted_on` 가드(과거월 재실행 미래제출 누출)·care_month YYYY-MM 검증·음수
  반송금액 거부·마스터코드 EP truncated+next_after keyset 페이지네이션(5000행 침묵절단 → HIS 영구 미매핑 방지).
- **검토 clean 확인**: 지급 전표 DR/CR 균형·멱등(uq)·variance GL 미전기·재청구 단일체인·이중소스 부호·
  learning dept 경계·인바운드 핸들러 멱등/savepoint·토요가산 기본 OFF.

## 2. 공단 지급 대사 강화 — v1.22.0 (#642 · 마이그 0161)

검토에서 정책/회계 결정 필요로 분리된 2건 — 사용자 확정 후 구현.

- **ADMIN 전용 게이팅**: `POST /claims/payments`가 claims.review/write(MANAGER DEPT 포함)로 부서장이
  기관 전체 현금전표(DR1110/CR1140)+배치 상태 일괄변경 가능하던 갭 → 전용 정책
  `("claims.payments","write")={ADMIN}` 분리(GL 현금전기 EP 관례 정합). 조회는 claims.review read 유지.
- **과다지급 선수금 분리·상계**: paid>결정잔여 초과분이 1140을 음수(대변잔액)로 오표시 → 초과분 CR
  **2141 선수금(공단)**(신설·2140은 검진권 전용) 분리 전기. 이후 결정 도착 시 인제스트(파서·JSON 양경로)가
  `reclass_advance` 자동 호출 — 2141 잔액을 목표 max(0,지급−결정)로 낮추는 DR2141/CR1140 델타(vN ref·멱등).
  reconciliation에 advance 노출·outstanding 클램프.

## 3. 출력물·레포트 공통 표기 정비 — v1.22.1 (#644)

공통 excel/pdf 모듈·PDF 템플릿 3종·xlsx 반출 9경로·범용 export 표기 상태 전수 검토.

- **[HIGH] 증명서 발급기관 "OO병원장" 견본 문구 하드코딩** → config ORG_NAME(env) 신설·미설정 폴백
  "본 의료기관장". 대외 공식 문서(재직/경력증명서)의 실질 무효 표기 해소.
- **[MED]** 계약서 org_name 훅 사문화(config 필드 부재로 항상 폴백) 해소 · 증명서 부서 ID 숫자("부서: 3")
  → 부서명 조인.
- **[LOW]** 금액 열 "(원)" 통일(시산표·현금흐름·기본금변동·워크리스트·의료비)·duty 시트명 한글화·
  Content-Disposition filename 인용 6곳 통일.
- **양호 확인**: 반출 접속기록(EXPORT/PRINT) 전 경로·천단위 콤마·ISO 날짜·autoescape·no-silent-caps.

## 4. 기관 설정 관리화면 — v1.23.0 (#646 · 마이그 0162)

env ORG_NAME은 재기동 필요 → **DB SoT + ADMIN 관리화면**으로 승격.

- **`common.org_settings` 단일행**(id=1 CHECK): 명칭(필수)·대표자·사업자등록번호·요양기관기호(문서
  표기용)·주소·전화. 우선순위 **DB > env ORG_NAME > 폴백** — 저장 즉시 반영(재기동 불요).
- **EP** GET/PUT `/admin/org-settings`: 정책 `admin.org` 신설(조회 DIRECTOR/ADMIN·저장 ADMIN)·
  source(db/env/unset) 노출·감사기록.
- **화면**: 시스템 → 기관 설정(`/erp/admin/org`) — "{기관명}장" 표기 미리보기. 화면 67→68.
- **소비처 연결**: 증명서 `{기관명}장`·계약서 발주자·급여명세 meta(신규). **hospital_code(env)는
  HIS/Clinic 연동 식별자로 의도적 분리**(화면에 경고 문구).
- ops-guide **§10** 개편(관리화면 우선·env 폴백).

## 5. prod 배포 — LIVE (2026-07-06, 2회)

1. **v1.22.0**: core+worker×2 재빌드. 검증 — healthy·alembic 0161·계정 2141 생성·EP 401·에러 0·PG 85/200.
2. **v1.22.1+v1.23.0**: core+worker×2+**web** 4컨테이너 재빌드(rsync 삭제 0건·변경 17+6 파일). 검증 —
   healthy·alembic **0162**·org_settings 테이블(0행)·EP 401·web `/erp/admin/org` 200·에러 0·PG 70/200.

## 6. 잔여

- **운영 1단계**: 관리자가 `/erp/admin/org`에서 기관 정식명칭 입력(현재 미설정 → 문서 "본 의료기관(장)" 폴백).
- **백로그(LOW)**: refresh_status 동시지급 lost-update(파생 필드·자가치유)·list_batches dept 미필터(기존동작)·
  세무 신고서식 xlsx 상호/사업자번호 헤더행(서식 확장 시).
- **기존 잔여(변동 없음)**: 환불 amend 강제(설계)·EDI G1·품목 매핑 회신(HIS 조율)·운영 활성화(발생주의
  cutover 회계사·MEDLT 실전문·BI Grafana).
