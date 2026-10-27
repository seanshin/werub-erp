"""RBAC 정책 테이블 + 행 수준 scope — Plan §3.3 매트릭스의 코드화 (design §6.2).

규칙: "자동 주입" 단독 의존 금지 — 조회 서비스는 caller_scope를 필수 인자로 받아
집계·raw SQL에도 명시적 WHERE를 합성한다.
"""
from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    DIRECTOR = "DIRECTOR"  # 원장
    ADMIN = "ADMIN"        # 관리자
    MANAGER = "MANAGER"    # 부서장
    STAFF = "STAFF"        # 직원
    WORKSTATION = "WORKSTATION"  # 재고 워크스테이션 일일 출입증(알바) — scm 전용 제한
    # 도메인 관리자(요청) — 각 도메인 전권, 타 도메인/시스템 관리 불가(최소권한)
    IT_MANAGER = "IT_MANAGER"              # IT관리자 — itam.*
    FINANCE_MANAGER = "FINANCE_MANAGER"    # 회계관리자 — finance.*(세무·원가 포함)
    LOGISTICS_MANAGER = "LOGISTICS_MANAGER"  # 물류관리자 — scm.*(재고·발주·마약류)


class ScopeKind(str, Enum):
    ALL = "ALL"
    DEPT = "DEPT"   # 본부서
    SELF = "SELF"   # 본인
    NONE = "NONE"   # 접근 불가


# 경영 BI(Grafana/Metabase) 접근 역할 — 공용 세션/대시보드라 인증만으론 부족(인가 게이트).
# Metabase(bi_sso)·Grafana(auth/bi-check auth_request)·사이드바(layout.tsx)가 동일 기준 사용.
BI_ROLES = frozenset({"ADMIN", "DIRECTOR", "MANAGER", "FINANCE_MANAGER"})


@dataclass(frozen=True)
class Scope:
    kind: ScopeKind
    dept_id: int | None = None
    user_id: int | None = None


# (resource, action) -> {role: ScopeKind}
# action: read | write | approve | export
# Plan §3.3 RBAC 매트릭스와 1:1 — 변경 시 Plan 문서와 동기화할 것
# 전 직원(내부 인증) 부여 — WORKSTATION(일일 출입증 kiosk·비직원)은 제외. enum-wide 부여의
# footgun(WORKSTATION 쓸어담기) 방지용 공용 상수.
_ALL_STAFF = {r: ScopeKind.ALL for r in Role if r != Role.WORKSTATION}

POLICY: dict[tuple[str, str], dict[Role, ScopeKind]] = {
    ("hr.employees", "read"):   {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                 Role.MANAGER: ScopeKind.DEPT, Role.STAFF: ScopeKind.SELF},
    ("hr.employees", "write"):  {Role.DIRECTOR: ScopeKind.NONE, Role.ADMIN: ScopeKind.ALL,
                                 Role.MANAGER: ScopeKind.NONE, Role.STAFF: ScopeKind.NONE},
    ("hr.payroll", "write"):    {Role.ADMIN: ScopeKind.ALL},
    ("hr.payroll", "read"):     {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                 Role.STAFF: ScopeKind.SELF},
    # 보수 프로필(기본급·계좌) — 급여보다 민감: 본인 SELF 조회도 불허, 관리자 전용
    ("hr.compensation", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("hr.compensation", "write"): {Role.ADMIN: ScopeKind.ALL},
    # 인사평가(Tier3·0229) — read: 관리자ALL·부서장DEPT·직원SELF(본인 평가). write(차수·배정·확정):
    # 관리자ALL·부서장DEPT(자기 부서 채점). 자기평가·평가자 채점은 서비스에서 행 검증(본인/평가자).
    ("hr.evaluation", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                 Role.MANAGER: ScopeKind.DEPT, Role.STAFF: ScopeKind.SELF,
                                 Role.IT_MANAGER: ScopeKind.SELF, Role.FINANCE_MANAGER: ScopeKind.SELF,
                                 Role.LOGISTICS_MANAGER: ScopeKind.SELF},
    ("hr.evaluation", "write"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                 Role.MANAGER: ScopeKind.DEPT},
    # 채용 ATS(공고·지원자·전형) — 지원자 개인정보(이름·연락처) 포함이라 인사 관리 한정
    # (원장·관리자·부서장). STAFF/도메인관리자 미노출.
    ("hr.recruit", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                              Role.MANAGER: ScopeKind.ALL},
    ("hr.recruit", "write"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                              Role.MANAGER: ScopeKind.ALL},
    # 복리후생(Tier3·0230) — read: 관리자ALL·부서장DEPT·그 외 SELF(본인 잔액/신청). write(신청)=SELF
    # 전직원. admin(제도·부여·승인·지급)=관리자. 신청은 서비스에서 본인 강제.
    ("hr.welfare", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                              Role.MANAGER: ScopeKind.DEPT, Role.STAFF: ScopeKind.SELF,
                              Role.IT_MANAGER: ScopeKind.SELF, Role.FINANCE_MANAGER: ScopeKind.SELF,
                              Role.LOGISTICS_MANAGER: ScopeKind.SELF},
    ("hr.welfare", "write"): {r: ScopeKind.SELF for r in Role if r != Role.WORKSTATION},
    ("hr.welfare", "admin"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("hr.leaves", "approve"):   {Role.MANAGER: ScopeKind.DEPT},
    ("hr.leaves", "read"):      {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                 Role.MANAGER: ScopeKind.DEPT, Role.STAFF: ScopeKind.SELF},
    ("hr.duties", "write"):     {Role.ADMIN: ScopeKind.ALL, Role.MANAGER: ScopeKind.DEPT},
    ("hr.duties", "read"):      {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                 Role.MANAGER: ScopeKind.DEPT},
    ("claims.review", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                 Role.MANAGER: ScopeKind.DEPT},
    ("claims.review", "write"): {Role.MANAGER: ScopeKind.DEPT, Role.ADMIN: ScopeKind.ALL},
    # 사전심사 룰 변경 — 적발 기준 자체를 바꾸므로 ADMIN 전용 (조회는 claims.review)
    ("claims.rules", "write"):  {Role.ADMIN: ScopeKind.ALL},
    # 공단 지급 대사 기록 — 기관 단위 GL 현금입금 전기(DR1110/CR1140)+배치 상태 일괄변경이라
    # 부서 차원이 없음. GL 현금전기 EP 관례(finance.settlement·patient.closing)와 동일하게 ADMIN 전용
    # (조회·대사요약은 claims.review read 유지).
    ("claims.payments", "write"): {Role.ADMIN: ScopeKind.ALL},
    # IT 자산관리(ITAM) — IT 관리자 통제. 자산·계약·증빙 접근(사진·계약서·PHI·라이선스키 포함)이라
    # 부서 차원 없이 ADMIN/DIRECTOR 전용(조회도 ALL만).
    ("itam.assets", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("itam.assets", "write"): {Role.ADMIN: ScopeKind.ALL},
    # 전자세금계산서 수취·대사 (A-1) — 증빙 통제: 등록·대사는 ADMIN
    ("finance.taxinvoice", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("finance.taxinvoice", "write"): {Role.ADMIN: ScopeKind.ALL},
    # 참조 마스터(약가·수가·고시)·환자안전 — 전 직원 열람이나 WORKSTATION(일일 출입증 알바)은 제외.
    # enum-wide {r for r in Role}는 WORKSTATION/도메인관리자까지 쓸어담는 footgun(전체검토 발견): kiosk가
    # 관리자 네임스페이스·환자안전사고(전원 민감)에 접근하던 것을 _ALL_STAFF(비-kiosk)로 교정.
    ("regulatory.master", "read"):  _ALL_STAFF,
    ("regulatory.master", "write"): {Role.ADMIN: ScopeKind.ALL},
    ("regulatory.notices", "write"): {Role.ADMIN: ScopeKind.ALL},
    # 환자안전사고 보고(환자안전법) — 보고는 전 직원(자율보고·kiosk 제외), 검토·종결은 관리자/부서장
    ("regulatory.safety", "read"):    _ALL_STAFF,
    ("regulatory.safety", "write"):   _ALL_STAFF,
    ("regulatory.safety", "approve"): {Role.ADMIN: ScopeKind.ALL, Role.MANAGER: ScopeKind.ALL,
                                       Role.DIRECTOR: ScopeKind.ALL},
    # 감염관리 지표(인증평가·HAI·손위생·항생제·다제내성균) — 조회는 전 직원(지표 공유),
    # 입력·목표설정은 감염관리실(부서장)·관리자(regulatory.infection write).
    ("regulatory.infection", "read"):  _ALL_STAFF,
    ("regulatory.infection", "write"): {Role.ADMIN: ScopeKind.ALL, Role.MANAGER: ScopeKind.ALL,
                                        Role.DIRECTOR: ScopeKind.ALL},
    # 의료기관 인증평가 조사항목 관리(상시준비) — 조회 전 직원, 항목·상태 관리는 품질팀(부서장)·관리자.
    ("regulatory.accreditation", "read"):  _ALL_STAFF,
    ("regulatory.accreditation", "write"): {Role.ADMIN: ScopeKind.ALL, Role.MANAGER: ScopeKind.ALL,
                                            Role.DIRECTOR: ScopeKind.ALL},
    # 컴플라이언스 만기·한도 통합 뷰 — 전사 크로스도메인 집계라 부서 무관 전 직원의 면허·계약·특히
    # 방사선 피폭선량(민감 건강정보)을 담는다. app 계층이 도메인별 DEPT 스코프를 우회하므로 부서장
    # (MANAGER=DEPT 원칙)에게 전사 노출은 최소권한 위반 → 원장·관리자 전용(감사·보존 뷰와 동일 등급).
    ("regulatory.compliance", "read"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    # 방사선 종사자 피폭선량 = 개인 건강정보 — 종전 regulatory.master read(전 직원)로 전 종사자 선량이
    # 보였다(2026-10-09 시나리오 메뉴·권한 정렬). 원장·관리자 전체·부서장 자기 부서·그 밖은 본인만.
    ("regulatory.radiation_dose", "read"): {
        **{r: ScopeKind.SELF for r in Role if r != Role.WORKSTATION},
        Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL, Role.MANAGER: ScopeKind.DEPT},
    # 데이터 보존연한 파기 대상 식별 — 민감(전 도메인 집계), ADMIN 전용
    ("admin.retention", "read"): {Role.ADMIN: ScopeKind.ALL},
    ("finance.vouchers", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("finance.vouchers", "write"): {Role.ADMIN: ScopeKind.ALL},
    # 계정과목(COA) 마스터 — 조회 원장·관리자, 설정(생성·수정·비활성)은 관리자 전용
    ("finance.accounts", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("finance.accounts", "write"): {Role.ADMIN: ScopeKind.ALL},
    # 예산 — 부서장 본부서 조회 (Plan §3.3, 정책 차용 금지 — 분석 M4)
    ("finance.budgets", "read"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                  Role.MANAGER: ScopeKind.DEPT},
    ("finance.budgets", "write"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    # 회계 기간 마감·연말 결산 — 전용 정책 (보안 점검 — finance.periods 미등록 403 수정)
    ("finance.periods", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("finance.periods", "write"): {Role.ADMIN: ScopeKind.ALL},
    # 마감 재개(확정기간 잠금해제)는 마감(write)과 분리된 상위 권한 — DIRECTOR 승인 가능(마감통제 분리·G2 심화)
    ("finance.periods", "approve"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    # 재무제표·고정자산·채권채무·경영분석 — 전용 정책 (vouchers 차용 금지, code-analyzer C-2)
    ("finance.statements", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    # write — 범용 첨부(entity_type="other") 업로드·삭제 통제. 미등록 시 전원 403(죽은 경로)이던 결함 보완
    ("finance.statements", "write"): {Role.ADMIN: ScopeKind.ALL},
    ("finance.assets", "read"):      {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("finance.assets", "write"):     {Role.ADMIN: ScopeKind.ALL},
    ("finance.settlement", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("finance.settlement", "write"): {Role.ADMIN: ScopeKind.ALL},
    # 경비 청구(Tier1 지출·경비) — 기안=전 직원 SELF·조회 부서장 DEPT·지급/카드정산=admin 액션
    ("finance.expenses", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                    Role.MANAGER: ScopeKind.DEPT, Role.STAFF: ScopeKind.SELF,
                                    Role.IT_MANAGER: ScopeKind.SELF,
                                    Role.LOGISTICS_MANAGER: ScopeKind.SELF},
    ("finance.expenses", "write"): {Role.DIRECTOR: ScopeKind.SELF, Role.ADMIN: ScopeKind.ALL,
                                    Role.MANAGER: ScopeKind.SELF, Role.STAFF: ScopeKind.SELF,
                                    Role.IT_MANAGER: ScopeKind.SELF,
                                    Role.LOGISTICS_MANAGER: ScopeKind.SELF},
    ("finance.expenses", "admin"): {Role.ADMIN: ScopeKind.ALL},   # 지급·법인카드 마스터/정산
    # 경비 승인(감사 M5) — 재무 도메인 한정(IT/물류 관리자 제외). MANAGER는 본부서(DEPT)만.
    ("finance.expenses", "approve"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                      Role.FINANCE_MANAGER: ScopeKind.ALL,
                                      Role.MANAGER: ScopeKind.DEPT},
    # 부대사업 수익(의료외수익) — read 재무역할·write(수납·항목마스터) ADMIN·FINANCE_MANAGER(현금전기 통제)
    ("finance.ancillary", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                     Role.MANAGER: ScopeKind.ALL,
                                     Role.FINANCE_MANAGER: ScopeKind.ALL},
    ("finance.ancillary", "write"): {Role.ADMIN: ScopeKind.ALL,
                                     Role.FINANCE_MANAGER: ScopeKind.ALL},
    ("analytics.reports", "read"):   {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    # SCM — 일반 재고와 마약류 분리 (분석 H3/M2: read/write·resource 명시)
    # WORKSTATION(일일 출입증·알바) — 재고 워크스테이션 작업만 허용(전체 ERP 접근 불가)
    ("scm.items", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                             Role.MANAGER: ScopeKind.ALL, Role.WORKSTATION: ScopeKind.ALL},
    ("scm.items", "write"): {Role.ADMIN: ScopeKind.ALL, Role.MANAGER: ScopeKind.DEPT,
                             Role.WORKSTATION: ScopeKind.ALL},
    # WORKSTATION 제외 — 일일 출입증은 재고 입고(scm.items)만. 구매발주·계약·결재는 정규 직원만
    # (보안검토 2026-06-29: 키오스크 PO 생성·승인·전권 우회 + WorkstationPass.id↔users.id 충돌 차단).
    ("scm.po", "read"):     {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                             Role.MANAGER: ScopeKind.ALL},
    ("scm.po", "write"):    {Role.ADMIN: ScopeKind.ALL, Role.MANAGER: ScopeKind.DEPT},
    # 구매요청(PR)·자재청구 — read 전직원·write(요청·회수) SELF·approve(승인·전환·불출) 관리자/부서장/물류
    ("scm.requisition", "read"):    {r: ScopeKind.ALL for r in Role if r != Role.WORKSTATION},
    ("scm.requisition", "write"):   {r: ScopeKind.SELF for r in Role if r != Role.WORKSTATION},
    ("scm.requisition", "approve"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                     Role.MANAGER: ScopeKind.DEPT,
                                     Role.LOGISTICS_MANAGER: ScopeKind.ALL},
    ("scm.narcotics", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                 Role.MANAGER: ScopeKind.DEPT},
    ("scm.narcotics", "write"): {Role.ADMIN: ScopeKind.ALL, Role.MANAGER: ScopeKind.DEPT,
                                 Role.WORKSTATION: ScopeKind.ALL},
    # 원무 — read/write 분리 (분석 H1: write 미정의로 read 권한 우회되던 결함)
    ("patient.patients", "read"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                   Role.MANAGER: ScopeKind.DEPT},
    ("patient.patients", "write"): {Role.ADMIN: ScopeKind.ALL},
    # 일마감 — 수납 입력(patient.patients)과 분리한 회계 통제 행위(전표 생성·일자 잠금).
    # read=이력·일계 미리보기, write=일마감, approve=마감 후 정정(역분개+재마감). 모두 ADMIN 통제.
    # 일마감은 병원 단위 회계(부서 차원 없음) — history/preview가 전부서 합산을 반환하므로
    # MANAGER(DEPT) 부여는 과다노출(전사 매출 열람). ALL 역할(DIRECTOR/ADMIN) 전용으로 제한.
    ("patient.closing", "read"):    {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("patient.closing", "write"):   {Role.ADMIN: ScopeKind.ALL},
    ("patient.closing", "approve"): {Role.ADMIN: ScopeKind.ALL},
    # 환자 민원·CS(VOC·Tier3 ①) — 접수(write)는 전 직원 창구 업무·본문은 민감(read 행스코프)·
    # 배정/회신/종결(admin)은 관리자·부서장(귀속부서). WORKSTATION(키오스크) 제외.
    ("patient.voc", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                               Role.MANAGER: ScopeKind.DEPT, Role.STAFF: ScopeKind.SELF,
                               Role.IT_MANAGER: ScopeKind.SELF,
                               Role.FINANCE_MANAGER: ScopeKind.SELF,
                               Role.LOGISTICS_MANAGER: ScopeKind.SELF},
    ("patient.voc", "write"): {r: ScopeKind.SELF for r in Role if r != Role.WORKSTATION},
    ("patient.voc", "admin"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                               Role.MANAGER: ScopeKind.DEPT},
    # 진료의뢰·회송(진료협력센터) — 기록에 환자식별자(chart_no)·환자명·진료사유(PII)가 담겨,
    # 전 직원 노출은 과다(적대검토 v1.238). 진료협력 관리 기능으로 원장·관리자·부서장 한정
    # (STAFF 담당자는 /admin/users 역할별 메뉴 DB오버라이드로 부여). VOC(SELF/DEPT)와 등급 정합.
    ("patient.referral", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                    Role.MANAGER: ScopeKind.ALL},
    ("patient.referral", "write"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                    Role.MANAGER: ScopeKind.ALL},
    ("admin.audit", "read"):    {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("admin.staging", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("admin.staging", "write"): {Role.ADMIN: ScopeKind.ALL},
    # ERP 내부 권한 배분 — HIS는 ERP 접근(SSO)만 부여, 내부 역할은 ERP 관리자가 별도 관리
    ("admin.users", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("admin.users", "write"): {Role.ADMIN: ScopeKind.ALL},
    # 기관 설정(공식 문서 발급기관 표기) — 조회는 원장 포함, 변경은 ADMIN
    ("admin.org", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("admin.org", "write"): {Role.ADMIN: ScopeKind.ALL},
    # 결정 대기함(0226) — 사람 결정 항목 관리. 결정은 경영 판단이라 DIRECTOR도 write.
    ("admin.decisions", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("admin.decisions", "write"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    # 정기업무(주/월/분기) 레지스트리·실행 — 조회는 원장 포함, 정의변경·실행·완료처리는 ADMIN(운영)
    ("admin.recurring", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL},
    ("admin.recurring", "write"): {Role.ADMIN: ScopeKind.ALL},
    ("common.departments", "read"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                      Role.MANAGER: ScopeKind.ALL},  # 조직구조 열람(관리자·매니저)
    ("common.departments", "write"): {Role.ADMIN: ScopeKind.ALL},  # 조직 관리 (Gap-6)
    # N단계 결재선 — 룰 설정=관리자, 결재/워크리스트 열람=결재 가능 역할(원장·관리자·부서장)
    ("common.approvals", "read"):  {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                    Role.MANAGER: ScopeKind.ALL},
    ("common.approvals", "write"): {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                    Role.MANAGER: ScopeKind.ALL},   # 결재 행위(룰 설정은 별도 게이트)
    ("common.approval_rules", "write"): {Role.ADMIN: ScopeKind.ALL},  # 결재선 룰 설정=관리자
    # 결재선 요청·관리(기안문서 선행) — R-1 ADMIN 겸임 거버넌스. read/write=전 직원(요청·개인결재선)·
    # admin(요청 승인·ORG 프로비저닝)=ADMIN. WORKSTATION 제외.
    ("common.approval_lines", "read"):  {r: ScopeKind.ALL for r in Role if r != Role.WORKSTATION},
    ("common.approval_lines", "write"): {r: ScopeKind.SELF for r in Role if r != Role.WORKSTATION},
    ("common.approval_lines", "admin"): {Role.ADMIN: ScopeKind.ALL},
    # 자금결재 기안문서(ERP 내부 결재) — read=본인 관여(SELF)·부서장 DEPT·관리자 ALL / write=전 직원 SELF(기안·상신·회수)
    # approve=전 직원(H-1 — 게이트는 넓게, 지정 결재자 판정은 엔진 _eligible; STAFF가 common.approvals에
    # 없어 결재 불가하던 문제 회피·경비 approve 게이트와 분리해 권한확대 폭발 차단).
    ("common.drafts", "read"):    {Role.DIRECTOR: ScopeKind.ALL, Role.ADMIN: ScopeKind.ALL,
                                   Role.MANAGER: ScopeKind.DEPT, Role.STAFF: ScopeKind.SELF,
                                   Role.IT_MANAGER: ScopeKind.SELF,
                                   Role.FINANCE_MANAGER: ScopeKind.SELF,
                                   Role.LOGISTICS_MANAGER: ScopeKind.SELF},
    ("common.drafts", "write"):   {r: ScopeKind.SELF for r in Role if r != Role.WORKSTATION},
    ("common.drafts", "approve"): {r: ScopeKind.ALL for r in Role if r != Role.WORKSTATION},
}


# ── 도메인 관리자 역할 스코프(요청) ────────────────────────────
# 각 도메인 resource에 read/write 부여(기존 매트릭스 비파괴·setdefault로 신규 역할만 추가).
# 세무(tax)·원가(costing)는 finance.* 키를, 재고(inventory)는 scm.* 키를 재사용(전용 키 없음).
# ★재검토 SoD(HIGH): 회계관리자 write에서 **기간마감(periods)·현금전기(settlement)는 제외**(ADMIN 유지)
#   — 준비(전표작성)와 승인/마감(기간잠금·현금전기)을 한 역할에 집중시키지 않음.
_FIN_READ = ("finance.taxinvoice", "finance.vouchers", "finance.accounts", "finance.budgets",
             "finance.periods", "finance.statements", "finance.assets", "finance.settlement",
             "finance.expenses")
_FIN_WRITE = ("finance.taxinvoice", "finance.vouchers", "finance.accounts", "finance.budgets",
              "finance.statements", "finance.assets",
              "finance.expenses")   # periods·settlement write 제외(경비 지급 admin도 ADMIN 전용)
# ★재검토(MED): 물류관리자 **마약류 write 제외**(마약류관리법 — 유자격 취급자/ADMIN·MANAGER 전용)
_SCM_READ = ("scm.items", "scm.po", "scm.narcotics")     # 조회는 마약류 포함(재고 감독)
_SCM_WRITE = ("scm.items", "scm.po")                     # 마약류 write 제외
_DOMAIN_MANAGER_GRANTS: dict[Role, list[tuple[str, str]]] = {
    Role.IT_MANAGER: [("itam.assets", "read"), ("itam.assets", "write")],
    Role.FINANCE_MANAGER: [(r, "read") for r in _FIN_READ] + [(r, "write") for r in _FIN_WRITE]
    + [("analytics.reports", "read")],
    Role.LOGISTICS_MANAGER: [(r, "read") for r in _SCM_READ] + [(r, "write") for r in _SCM_WRITE],
}
for _role, _keys in _DOMAIN_MANAGER_GRANTS.items():
    for _key in _keys:
        POLICY.setdefault(_key, {})[_role] = ScopeKind.ALL
# 공통: 도메인 관리자도 부서 조회(피커)·결재 참여(자기 도메인 문서). 결재선 룰 설정은 ADMIN 유지.
for _role in (Role.IT_MANAGER, Role.FINANCE_MANAGER, Role.LOGISTICS_MANAGER):
    POLICY.setdefault(("common.departments", "read"), {})[_role] = ScopeKind.ALL
    POLICY.setdefault(("common.approvals", "read"), {})[_role] = ScopeKind.ALL
    POLICY.setdefault(("common.approvals", "write"), {})[_role] = ScopeKind.ALL


def resolve_scope(resource: str, action: str, *, role: Role,
                  dept_id: int | None, user_id: int) -> Scope:
    """정책 평가 — 매트릭스 미등재 또는 NONE이면 접근 불가."""
    grants = POLICY.get((resource, action), {})
    kind = grants.get(role, ScopeKind.NONE)
    if kind == ScopeKind.NONE:
        return Scope(ScopeKind.NONE)
    if kind == ScopeKind.DEPT:
        return Scope(ScopeKind.DEPT, dept_id=dept_id)
    if kind == ScopeKind.SELF:
        return Scope(ScopeKind.SELF, user_id=user_id)
    return Scope(ScopeKind.ALL)


def require_scope(user, resource: str, action: str) -> Scope:
    """단일 권한 게이트 (분석 M5 — 라우터별 _require 중복·기본값 오용 제거).

    NONE이면 ForbiddenError. resource/action 기본값 없음 — 호출부 명시 강제.
    """
    from erp.errors import ForbiddenError
    scope = resolve_scope(resource, action, role=Role(user.role),
                          dept_id=user.dept_id, user_id=user.user_id)
    if scope.kind == ScopeKind.NONE:
        raise ForbiddenError()
    return scope


def require_license(user) -> None:
    """면허 게이트(FR-47) — 면허 만료(license_flag=False) 시 면허필요 임상행위 차단.

    적용 범위(실제): 마약류 취급·정정(scm.narcotics write). ERP 내 휴가·성과급·전표 등
    "결재"는 임상 면허와 무관하므로 본 게이트 비대상이며, 전자결재 본체는 외부 그룹웨어가
    수행(연동·콜백). 면허 필요한 임상 액션이 추가되면 그 지점에 본 게이트를 덧댄다.

    동작: license_flag는 면허 만료자에 한해 False(만료자 재로그인 후 lic=False). 비면허
    직군은 True(만료할 면허 없음)이므로 통과하나 임상 액션은 RBAC가 별도 차단.
    fail-open(lic 클레임 부재 시 통과)은 의도 — 만료 전 발급 토큰/비임상 사용자의 가용성
    보장. 만료 즉시 강제하려면 token_version 무효화로 재로그인을 유도(현 운영방식).
    """
    from erp.errors import ForbiddenError
    if not getattr(user, "license_flag", True):
        raise ForbiddenError("의료 면허 만료(또는 미검증) — 갱신 후 가능합니다")
