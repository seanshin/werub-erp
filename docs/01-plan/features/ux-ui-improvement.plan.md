# UX·UI 개선 기획서 (v1.78.0 기준)

- 작성일: 2026-07-13 · 상위 문서: `docs/01-plan/full-system-remediation.plan.md`(P0-5/6/7·P2-9/10/11·P3-8/9를 본 기획으로 세분화·흡수)
- 조사 방법: 3갈래 병렬 심층 감사 — ①디자인 시스템·시각 일관성(77화면·프리미티브 전수) ②내비게이션/IA·업무 워크플로우 동선(메뉴 카탈로그↔라우트 대조·5대 업무 흐름 추적) ③핵심 10화면 상세 UX(대시보드·전표·사전심사·원무·급여·ITAM·워크스테이션·kiosk·경영·사용자관리)

---

## 0. 진단 요약

**강점(보존·확산할 하우스 패턴)** — 재고 워크스테이션의 포커스 규율(autofocus·Enter/Tab·mutation 후 재포커스·GS1-128 자동파싱), 사전심사의 인라인 컴포저(⌘/Ctrl+Enter+힌트 표기)·탭 점프, ITAM의 상태 칩 필터·필터 보존 CSV, 경영 대시보드의 위젯 편집·2단 드릴·DescBox(산식/출처/해석), `useInputDialog`, Table의 숫자 자동 우측정렬, Modal의 접근성(포커스트랩/ESC/복원), 서버 주도 메뉴, Card+`space-y-4` 리듬. **코드베이스는 "관례적으로 일관·토큰 부재"** — 통합은 재설계가 아니라 이미 수렴한 패턴의 추출이다.

**약점(구조 요약)** —
1. **군도(archipelago) 구조**: 74페이지 중 `next/link` 사용 9개 · `router.push` 0 · `useSearchParams` 0. 크로스모듈 이동 = ID를 눈으로 읽고 → 사이드바 67개 중 탐색 → ID 수동 재입력.
2. **기능적 결함 6건**: UX 조사에서 발견됐으나 사실상 버그(데이터 오염·업무 완결 불가) — §1.
3. **피드백 체계 부재**: toast 0 · 성공/실패가 같은 파란 `<p>` 12파일+ · 모달 아래 가려지는 메시지 · 파괴적 액션 무확인 다수.
4. **디자인 토큰 0**: 시맨틱 컬러 충돌(같은 상태가 화면마다 다른 색) · Kpi 컴포넌트 6중 복제 · select 69개 12레시피 · 죽은 다크모드 코드.

---

## 1. UX-0 — 기능적 결함 (즉시 수정 권장·기획 승인 불요 수준) 🔴 ✅ **R0 완료(v1.79.0·#795)**

> UX 조사에서 발견됐으나 성격은 **버그**. **X0-1~5·7·8 완료**(v1.79.0). X0-6은 통제물질 원장 CHECK 마이그·컴플라이언스 설계 필요·DISPOSE 우회 존재로 별도 처리(보류).

| ID | 결함 | 근거 | 영향 | 수정 |
|---|---|---|---|---|
| X0-1 | **접수 진료과 "IM" 하드코딩** | `patients/page.tsx:48` `dept_code:"IM"` 고정 | **전 접수가 내과로 기록 = prod 데이터 오염 진행 중**(부서 스코프 RBAC·부문 손익·삭감통계 왜곡) | 진료과 select 추가(dept 목록 API 기존) |
| X0-2 | **수납 수단 "CARD" 하드코딩** | `patients/closing/page.tsx:48` | 현금 수납 불가 → 일마감 수단별 집계(현금 1110/카드 1130 분개) 왜곡 | 수단 select(CARD/CASH/TRANSFER) |
| X0-3 | **전표 승인에 금액·라인 미표시** | `finance/page.tsx:176-185` — 목록에 일자/소스/메모만 | **승인자가 금액을 못 보고 승인**(내부통제 실질 훼손) | 목록에 금액 컬럼+행 확장 라인 표시 |
| X0-4 | **세금계산서 대사 업무 완결 불가** | `finance-tax-invoices/page.tsx:74` 미지급 ID 입력 요구 ↔ `finance-settlement`는 ID 미표시 | 화면만으로 대사 불가능(curl 수준) | settlement에 ID 컬럼+대사 다이얼로그를 공급사 필터 미지급 픽커로 |
| X0-5 | **마감 후 정정(amend) dead limb** | `closing/page.tsx:67` `correction_payments:[]` 상시 | 정정 사유만 받고 정정 내용 전달 불가 | 정정 라인 입력 구현 또는 카드 제거(결정) |
| X0-6 | **마약류 조정 음수 불가** | `inventory-workstation.tsx:187` 수량>0 강제가 ADJUST에도 적용 | 재고 하향 조정을 UI에서 못 함 | ADJUST 모드만 음수 허용 |
| X0-7 | **고아 화면 2종** | `/admin/payroll-settings`(메뉴·링크 0)·`/assets/my`(수령확인 ESS — 도달 불가) | 급여설정=URL 직타만·자산 수령확인 흐름 단절 | menu_service.py 카탈로그 2줄 추가 |
| X0-8 | **도메인 관리자 기본 메뉴 결핍** | `menu_service.py:13` `_ALL`이 FINANCE/IT/LOGISTICS_MANAGER 제외 → 자기 급여명세·**자기 2FA 등록(/security)**·대시보드 미노출 + 로그인 랜딩=/dashboard(빈 화면·사이드바 하이라이트 없음) + `layout.tsx:76` BI 링크 role 목록에 FINANCE_MANAGER 누락(서버 BI_ROLES와 불일치 확정) | 신규 3역할 사용자의 첫 경험 파탄·2FA 등록 불가 | `_ALL`에 3역할 포함(또는 기본세트 부여)+BI 링크 서버 기반화+역할별 랜딩(FINM→/executive) |

---

## 2. UX-1 — 피드백·안전장치 (최고 레버리지·선행 필수)

### X1-1. Toast 시스템 도입 [M]
- 현상: toast 0건. `setMsg` 로컬 상태 ~39파일 — **12파일+에서 성공·실패가 같은 파란 텍스트**(`costing-pl:36,43`·`finance-settlement:47,96`·`finance-leases`·`admin/org:88` 등). 긴 페이지에선 메시지가 화면 밖. 사전심사 등 **모달 안 액션의 성공 메시지가 모달 뒤에** 렌더(`pre-review:219`).
- 조치: `(app)/layout.tsx`에 토스트 프로바이더(포털·tone별 색·자동소멸) + `useMutationToast` 헬퍼 → 39파일의 `onSuccess/onError→setMsg` 보일러플레이트 일괄 치환. `alert()` 3건도 흡수. MessageBanner는 지속 상태 전용으로 존치.

### X1-2. ConfirmDialog + Modal dirty 가드 [S~M]
- 현상: `window.confirm` 10사이트(퇴직 확정·자산 폐기·마감 등 파괴적 액션) — 브라우저 억제 시 확인 자체가 불가. **admin/users 역할 변경이 select onChange 즉시 PATCH·무확인**(`admin/users:48-52` — 스크롤 미끄러짐 한 번에 권한 변경+강제 재인증, 실패 시 uncontrolled select가 거짓 표시). **Modal 오버레이 클릭이 무조건 닫힘** — ITAM 24필드 폼이 한 번의 오클릭으로 전량 소실(`ui/index.tsx:181`).
- 조치: 기존 `input-dialog.tsx` 확장으로 ConfirmDialog → 10사이트+역할변경/활성토글 적용. Modal에 `dirty` prop(오버레이/ESC 닫기 시 확인).

### X1-3. Button·로딩 개선 [S]
- `isLoading`이 라벨을 "처리 중…"으로 치환(폭 점프·맥락 소실) → 스피너+라벨 유지. `size`·`ghost` variant·`ButtonLink`(Link 스타일 드리프트 해소).

### X1-4. 신규 행 하이라이트 [S]
- 생성 성공 후 목록 갱신만 되고 시선 유도 없음(전표·환자·계약) → invalidate 후 새 행 일시 배경 하이라이트.

---

## 3. UX-2 — 연결성 (군도 해소)

### X2-1. URL 상태 인프라 [M·선행]
- `useSearchParams` 0 → 탭·필터·선택 엔티티가 새로고침에 소실·공유 불가("이 케이스 봐주세요"가 URL이 안 됨). 알림 딥링크(X2-3)·대시보드→필터뷰 링크의 전제.
- 조치: 공용 `useUrlState` 훅 → 사전심사(케이스 id·탭)·ITAM(자산 id)·마감(일자)·워크리스트 필터부터 적용.

### X2-2. 참조 양방향 내비게이션 [M]
- 전표 `source_ref`→원천 화면(PO/마감/급여), 마감 `voucher_ref`→/finance, PO RECEIVED→미지급→전표 체인, 사전심사 케이스↔삭감 명세(차트/명일련 키). 현재 전부 죽은 mono 텍스트.

### X2-3. 알림 딥링크 [M·백엔드 협업]
- `Notification` 모델에 href/entity 필드 없음(`common/models.py:89-100`) → "이의신청 D-7" 알림을 받아도 사이드바→탐색→ID 재입력. 모델 필드 추가(마이그)+발신 8사이트에 링크 주입+알림센터·벨 드롭다운 링크 렌더. 타입별: APPEAL_DEADLINE→`/claims?appeal=`·STOCK→`/inventory?item=`·APPROVAL→결재함(X2-4).

### X2-4. 통합 결재함 [M]
- 승인이 4곳에 산재(전표=/finance·PO=/inventory·ITAM 폐기=모달 속·계약=별도) — "내 결재 대기 N건" 표면 부재. 단일 결재함 화면(+대시보드 위젯): PENDING 전표·REQUESTED PO·ITAM 폐기 스텝·CLM/의료계약 집계, APPROVAL 알림이 여기로 링크.

### X2-5. 픽커 일반화 — ID 수동 입력 근절 [M]
- 원시 ID 텍스트 입력 현황: 급여 직원 ID(`hr/payroll:80` — EmployeePicker가 있는데 ITAM만 사용, 그마저 `/itam/employees` 엔드포인트 의존)·재고 입고 품목 ID(`inventory:154`)·이의신청 #(`claims:161` — 3cm 위 표에서 읽어 재입력)·미지급 ID(X0-4)·계정코드 bare 텍스트(placeholder "1110"·`finance:146`).
- 조치: EmployeePicker를 HR 디렉터리 EP로 일반화 + ItemPicker/AccountPicker(코드→명 에코)/SupplierPicker 신설 → 급여·전표 라인·입고·발주 보드(공급사·품목 **이름 표시** — 현재 승인자가 `supplier_id` 숫자와 `#item_id×qty`만 보고 승인)에 배선.

### X2-6. 이름 표시 일반화 [M·일부 백엔드]
- 숫자 ID 노출: 담당 `#{assignee_id}`(사전심사)·comments `#{author}`·budget `department_id`(`finance:119`)·ITAM `owner_department_id`(`assets/it:571`)·inspect `supplier_id`. 서버 조인 이름 동봉(기존 `assigned_employee_name` 패턴 확산) 또는 공용 `useNameMap`.

### X2-7. 지급대사 화면 신설 [M]
- 백엔드 `GET/POST /claims/payments`·`reconciliation` 완비(A~E 트랙)·**웹 화면 0** — 1140 생애주기의 돈 단계가 UI 데드엔드. care_month별 대사 목록+기록 폼+요약, /claims·대시보드에서 링크.

---

## 4. UX-3 — 디자인 시스템 통합

### X3-0. 토큰·위생(선행) [S]
- `globals.css @theme`: 시맨틱 컬러(success/warning/danger/info/neutral·chart-1..6)·**한국어 폰트 스택**(현 Arial)+`word-break: keep-all`(현 0건 — 좁은 셀 한글 중간 개행)·죽은 geist/다크모드 변수 제거(다크 OS 사용자에게 잠재 함정·명시적 go/no-go는 별도 결정, 내부 ERP라 no-go 권장). `lib/chart-palette.ts`(hex 팔레트 3중 정의 해소).

### X3-1. 시맨틱 상태 컬러 통일 [M]
- 확인된 충돌: **FAILED**=amber(`finance-voucher-status:28`) vs red(`admin/integrations:30`) — 같은 인박스/아웃박스 도메인에서 정반대 심각도 신호 · **APPROVED**=green(hr/claims) vs blue(`inventory:92`) · **CANCELED**=gray vs red · **green의 이중 의미**(ACTIVE도 green·CLOSED "마감"도 green) · SENT=green vs blue · 배지에 raw 영문 enum 노출(POSTED/ACTIVE/BLOCK) 화면 다수.
- 조치: `lib/status.ts` — 시맨틱 톤 맵(positive/active/waiting/danger/terminal-neutral)+도메인별 한글 라벨 사전 → `<StatusBadge>` 도입, Badge tone을 시맨틱으로(56파일 기계적 스윕·`green-*` 7건→emerald 통일).

### X3-2. 폼 프리미티브 [M]
- `Select`(69개 raw·12레시피·로컬 `const input=` 문자열 9파일 삭제)·`Field`(label+error+required — Input에 error prop 없음이 화면별 검증 표시 부재의 근원)·`Textarea`/`Checkbox`·**`MoneyInput`**(억 단위 금액이 raw `type="number"`·천단위 구분 0 — 22파일)·`DateRangeInput`/`MonthInput`(date 25·month 14파일 수제).

### X3-3. 페이지 스캐폴딩 [M]
- `PageHeader`(h1 66개 수제·3변형)·`FilterBar`(25파일 수제 행)·`KpiCard/KpiGrid`(**6중 복제** 로컬 컴포넌트·`tabular-nums` 통일·raw Tailwind 문자열 tone prop 제거)·`Tabs`(4파일 복제)·`Pagination`(5파일 복제).

### X3-4. Table v2 [L·단계적]
- 현 ceiling: **sort 없음(전 화면 정렬 불가)·sticky header 없음·금액 합계 footer 없음·row click 없음·서버 페이지네이션 훅 없음**(size=50/100 절단 10화면 — 재무 증적 완전성 문제). `sortable`·`stickyHeader`·`footer`·`onRowClick`·컬럼 정의 옵션 추가 → 고빈도 목록(전표·워크리스트·ITAM)부터 이관. 숫자 자동 우측정렬은 보존.

### X3-5. 타이포·밀도 정리 [S]
- 12px 미만 5종 혼재(`text-[10px]`23·`[11px]`135·`[13px]`10) → 3단계 소형 스케일 문서화. KPI 값 클래스 통일. h2 부재로 평탄한 문서 아웃라인(a11y) 보완.

---

## 5. UX-4 — IA·내비게이션

### X4-1. 사이드바 재편 [M·menu_service.py]
- 현상: 9그룹 67항목 **접기 없음**(ADMIN=한 스크롤 열)·재무 18항목 메가그룹(원가 3종 포함)·인사 15항목 무질서(관리자·셀프서비스 혼재)·**ITAM 4종이 "재고" 그룹**인데 고정자산은 "재무"(자산 이중 소속 혼란).
- 조치: 접이식 그룹(상태 영속)+재무→재무회계/원가/자금 분할+ITAM 독립 그룹+인사 생애주기 순 정렬+**"내 업무(ESS)" 그룹 신설**(내 급여·휴가·증명서·내 자산 수령확인·보안 — X0-7/8과 연동). `/admin/menu`는 가시성 토글만 가능(재편은 카탈로그 코드)이므로 함께 반영.

### X4-2. 웨이파인딩 [S]
- **브라우저 탭 타이틀이 전 화면 "병원 ERP"**(멀티탭 업무가 기본인데 N개 동일 탭) → 메뉴 라벨 기반 `document.title` 훅. 헤더에 사용자명·부서 표시(현 role 배지만 — 공용 워크스테이션 환경). h1↔메뉴 라벨 표기 드리프트 정리. 화면별 "?"→`/help#섹션` 컨텍스트 링크(help는 12영역 완비인데 화면에서 미연결).
- 브레드크럼은 라우트 평탄 구조(`finance-*` 14개 형제) 때문에 보류 — 라우트 개편(L)과 함께 후속.

### X4-3. 대시보드 액션화 [S]
- 하단 위젯 3종(이의신청 D-day·삭감통계·안전재고)이 표시 전용 → 행 링크 연결. STAFF 대시보드에 ESS 숏컷. dev 노트 노출 제거(`dashboard:180`). 저재고 위젯 `size=200` 클라 필터(200개 초과 침묵 누락)→서버 필터.
- 경영 대시보드: 레이아웃 localStorage 전용(기기 변경 시 소실·"기본값 복원" 무확인) → 서버측 사용자 설정 저장.

### X4-4. 모달→라우트 전환 [L·단계적]
- ITAM 자산 상세(806LOC 화면의 130LOC 모달="페이지인 척하는 모달")·사전심사 케이스 상세+명세 → `[id]` 서브라우트(빠른 확인용 모달은 유지). 링크 가능성(폐기 결재 알림→자산 직행)·북마크 가능성의 전제. X2-1과 결합.

---

## 6. UX-5 — 화면별 개선 (우선순위 순)

| 화면 | 개선 | 노력 |
|---|---|---|
| **급여(hr/payroll)** | 백엔드(법정명세 완비·251명 확정)와 격차 최대: ①run 단위 산정/일괄확정 뷰+확정 무확인 해소 ②EmployeePicker 배선 ③명세 지급/공제 소계+**PDF 출력**(법정 문서) ④calc_basis 표시 ⑤raw 영문 item_code 한글화 ⑥/admin/payroll-settings 연결 | M~L |
| **전표(finance)** | X0-3(금액·라인) + AccountPicker + `<form>` 시맨틱(Enter 동작 0) + 상태 한글화 + 일자/상태 필터·페이지네이션(size=50 고정) + budget 부서명 | M |
| **사전심사(pre-review)** | 케이스 라우트화(X4-4)+"all" 버튼 명명("검토+명세")+담당 지정 UI 부재 해소(updateCase는 지원)+차트필터 디바운스+벌크시트 enum 한글 안내 | M |
| **ITAM(assets/it)** | 24필드 평탄 폼 섹션화(기본/네트워크/보안/의료기기/일정)+상세 라우트화+검색 디바운스+비용 합계 | M |
| **admin/users** | X1-2(확인 다이얼로그)+controlled select+검색/필터+부서·직원 이름 | S |
| **워크스테이션** | 라벨 인쇄 confirm이 연속 스캔 흐름 차단 → 비차단 토스트 버튼/"항상 인쇄" 토글. inspect 공급사명 | S |
| **원무(patients)** | X0-1/2/5 + 검색 Enter 동작+생성 폼 reset+PayCell 미수금 프리필 | S~M |
| **경영(executive)** | 드릴 종점→운영 화면 점프(부서→해당 워크리스트)+월 ◀▶ 스테퍼+보드 인쇄 | S~M |

---

## 7. 실행 로드맵

| 단계 | 내용 | 산출 |
|---|---|---|
| **R0 (즉시·1릴리즈)** | UX-0 전건(X0-1~8) — 버그 성격·기획 무관 | 데이터 오염 중단·업무 완결성 복구 |
| **R1 (1~2주)** | X3-0 토큰 → X1-1 토스트 → X1-2 확인/dirty → X1-3/4 | 피드백 체계 확립(이후 모든 작업의 기반) |
| **R2 (2~3주)** | X2-1 URL 상태 → X2-5 픽커 → X2-2 참조 내비 → X2-6 이름 | ID 재입력·데드엔드 소멸 |
| **R3 (2~3주)** | X2-3 알림 딥링크(백엔드 마이그 포함) → X2-4 결재함 → X2-7 지급대사 화면 | 알림→행동 직결·승인 단일 표면 |
| **R4 (3~4주)** | X3-1 상태색 → X3-2 폼 → X3-3 스캐폴딩 → X3-5 | 시각 일관성(기계적 스윕 중심) |
| **R5 (2~3주)** | X4-1 사이드바 → X4-2 웨이파인딩 → X4-3 대시보드 | IA 완성 |
| **R6 (지속)** | X3-4 Table v2 → X4-4 라우트화 → §6 화면별(급여 최우선) | 심화 |

- 종속성: R1은 전 단계의 전제(치환 대상 패턴을 먼저 확정) · X2-3은 백엔드 마이그 1건 · X4-4는 X2-1 선행 · 테스트는 상위 계획 P1(HTTP 테스트)·P3-9(Playwright)와 동기화 — R2 이후 e2e가 URL 상태로 시나리오 작성 가능해짐.
- 검증: 각 단계 종료 시 `npm run build`+`tsc --noEmit`+상위 15화면 수동 시나리오. R0는 백엔드 회귀 테스트 동반(접수 dept·수납 method는 서버 계약 확인).

## 8. 완료 기준
- R0: prod 신규 접수의 dept_code 분포가 실제 진료과 분포를 반영·현금 수납 기록 존재·전표 승인 화면에서 금액 확인 가능
- R1: `setMsg` 파란 텍스트 패턴 0건·window.confirm 0건·모달 dirty 손실 0건
- R2: raw ID 텍스트 입력 0건·"화면에 없는 ID를 요구하는 다이얼로그" 0건
- R3: 알림 클릭→해당 엔티티 직행률 100%(신규 타입)·결재 대기 단일 화면 집계
- R4: 상태 배지 시맨틱 충돌 0건·화면별 로컬 스타일 문자열(`const input=`) 0건
- 전체: 사이드바 없이 업무 흐름(접수→수납→마감→전표, 사전심사→삭감→이의→지급대사)을 링크만으로 완주 가능
