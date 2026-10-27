"""역할별 메뉴 노출 — 코드 카탈로그(SoT) + DB 오버라이드(common.menu_permissions).

menu_key = 프론트 라우트 href. 기본 노출은 카탈로그 default_roles, DB 행이 있으면 그 값 우선.
프론트는 GET /menu(내 역할 가시 항목)로 렌더, 관리자는 매트릭스로 (menu×role) 토글.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.models import MenuPermission

ROLES = ["DIRECTOR", "ADMIN", "MANAGER", "STAFF",
         "IT_MANAGER", "FINANCE_MANAGER", "LOGISTICS_MANAGER"]
_ALL = frozenset({"DIRECTOR", "ADMIN", "MANAGER", "STAFF"})
_MGR = frozenset({"DIRECTOR", "ADMIN", "MANAGER"})
_FIN = frozenset({"DIRECTOR", "ADMIN"})
# 전 역할(도메인 관리자 포함) — 개인·범용 메뉴 전용. 도메인 관리자(IT/재무/물류)도 본인 급여명세·
# 2FA 등록·대시보드 등 개인 업무는 접근해야 하므로 _ALL(4역할)에서 누락되던 것을 교정(전체검토 X0-8).
_EVERYONE = _ALL | frozenset({"IT_MANAGER", "FINANCE_MANAGER", "LOGISTICS_MANAGER"})
# 결재 가능 역할 — common.approvals read와 정합(관리자·부서장·도메인관리자·STAFF/WORKSTATION 제외)
_APPROVERS = _MGR | frozenset({"IT_MANAGER", "FINANCE_MANAGER", "LOGISTICS_MANAGER"})
# 도메인 관리자 메뉴 노출(요청·#5) — 각 도메인 메뉴만 추가 노출(시스템 관리 메뉴 제외)
_IT = _FIN | {"IT_MANAGER"}                    # IT 자산 메뉴
_FINM = _FIN | {"FINANCE_MANAGER"}             # 재무·세무·원가·분석 메뉴
_LOGM = _MGR | {"LOGISTICS_MANAGER"}           # 재고·발주 메뉴

# 카탈로그 — (group, [(href, label, default_roles)]). href=menu_key. 프론트 NAV와 키 일치.
MENU_CATALOG: list[tuple[str | None, list[tuple[str, str, frozenset]]]] = [
    (None, [
        ("/dashboard", "대시보드", _EVERYONE),
        # 사용 설명서는 하단 도움말 링크로 접근(사이드바 메뉴에서는 제외)
        ("/executive", "경영진 대시보드", _FINM),
        ("/analytics", "경영분석 (BI)", _FINM)]),
    # "자금결재": ERP 내부 결재(지출·전표·구매·계약 등 자금 집행)를 그룹웨어 전자결재와 구분하는 명칭.
    # 결재함·기안·결재선을 한 그룹으로 — 개인 셀프서비스가 아니라 **업무 기능**이므로 분리(사용자 결정).
    ("자금결재", [
        ("/approvals", "자금결재함", _APPROVERS),   # X2-4 통합 결재함(내 결재 대기)
        ("/approvals-drafts", "자금결재 기안", _EVERYONE),
        ("/approvals-lines", "자금결재선 관리", _EVERYONE)]),
    # 개인 기능 — 전 직원이 '자기 것'을 보고 신청하는 화면만(IA·UX 기획서 §2). 전부 _EVERYONE·
    # 서버 권한은 각 도메인 스코프가 별도 강제(메뉴 노출≠권한). 각 화면은 도메인 그룹에서 이동.
    ("개인 기능", [
        ("/hr/payroll", "내 급여명세", _EVERYONE),
        ("/finance-expenses", "내 경비청구", _EVERYONE),
        ("/hr/attendance", "내 근태", _EVERYONE),
        ("/hr/leaves", "휴가 신청", _EVERYONE),
        ("/hr/certificates", "증명서 발급", _EVERYONE),
        ("/hr/evaluation", "인사평가(자기평가)", _EVERYONE),
        ("/hr/welfare", "복리후생 신청", _EVERYONE),
        ("/assets/my", "내 IT 자산", _EVERYONE),
        ("/security", "내 보안(2FA)", _EVERYONE)]),
    ("원무", [
        # 2026-10-09 메뉴·권한 정합: 셋 다 _ALL이라 STAFF에 노출됐지만 서버는 patient.closing
        # (DIRECTOR/ADMIN)·patient.patients(+MANAGER DEPT)로 거부해 빈 화면이었다 → 정책에 맞춤.
        # 원무 STAFF 담당자는 /admin/menu 오버라이드가 아니라 역할(정책) 차원에서 부여해야 한다.
        ("/patients/closing", "수납·일마감", _FIN),
        ("/patients/reservations", "예약", _MGR),
        ("/patients", "환자·접수", _MGR),
        ("/patients/receivables", "미수금 채권관리", _FIN),   # 미수연령·독촉·분납·대손·환불(patient.closing=DIRECTOR/ADMIN)
        ("/patients/voc", "민원·CS(VOC)", _EVERYONE),   # 접수=전 직원·처리 권한은 서버 게이트
        ("/patients/referrals", "진료의뢰·회송", _MGR)]),   # 진료협력센터·PII(chart_no·환자명) 관리한정
    # IA 재구성(v1.132) — 과밀 그룹 분할(인사17·재무18)·ITAM 독립·흩어진 세무/원가 통합.
    ("인사·조직", [
        ("/hr/employees", "직원(의료진)", _MGR),
        # 의료진 계약·일반직 근로계약 = 보수(hr.payroll read: DIRECTOR/ADMIN ALL·STAFF SELF·MANAGER 없음)
        # → _MGR이면 부서장에게 403 화면(2026-10-09 메뉴·권한 정합). 보수 정보라 권한 확대 대신 메뉴 축소.
        ("/hr/medical-contracts", "의료진 계약", _FIN),
        ("/hr/employment-contracts", "일반직 근로계약", _FIN),   # 근기법§17·비의료진(write ADMIN=hr.payroll)
        ("/hr/personnel-records", "직급·호봉/징계상벌", _FIN),   # 호봉표·상벌(hr.payroll·write ADMIN)
        ("/hr/recruitment", "채용 관리(ATS)", _MGR),   # 공고·지원자·전형(hr.recruit·PII 관리한정)
        ("/hr/absences", "휴직·퇴직 처리", _MGR),
        ("/hr/duties", "듀티", _MGR),
        ("/hr/labor-rules", "노동규제 룰(시프트·임계값)", _MGR)]),
        # 근태·휴가·증명서·내 IT자산은 "개인 기능"으로 이동
    ("급여·4대보험", [
        ("/hr/onboarding", "급여 기초정보", _FIN),
        ("/admin/payroll-settings", "급여 설정", _FIN),   # 고아 화면 연결(전체검토 X0-7)
        ("/hr/incentive", "의료진 성과급", _FIN),       # hr.payroll read ALL 전용(전 의료진 집계)
        ("/hr/statutory", "4대보험·연말정산", _FIN),    # hr.payroll read(MANAGER 없음)
        ("/hr/insurance-reports", "4대보험 신고", _FIN),
        ("/hr/retirement", "퇴직급여충당", _FIN),       # 전 직원 추계액 — hr.payroll read ALL 전용
        ("/hr/compliance", "법정교육·검진", _FIN),
        ("/hr/training", "원내 교육이수", _MGR)]),   # 사내 과정·회차·이수(Tier3·0228)
        # 내 급여명세·인사평가(자기평가)·복리후생 신청은 "개인 기능"으로 이동
    ("세무", [
        ("/tax", "원천징수·신고", _FINM),
        ("/finance-tax-invoices", "전자세금계산서", _FINM),
        ("/finance-withholding", "원천징수 요율", _FINM)]),   # IA — 세무로 이동(구 재무)
    ("의료보험", [
        ("/claims", "삭감·이의신청", _MGR),
        ("/claims/pre-review", "사전심사", _MGR),
        ("/claims/payments", "공단 지급 대사", _MGR),   # X2-7 지급대사 화면(백엔드 A~E 완비)
        ("/claims/review-rules", "심사 룰 관리(DUR·진단·묶음·가산)", _MGR),
        ("/claims/learning", "심사 학습 사례", _MGR)]),
    ("규제·컴플라이언스", [
        ("/compliance", "컴플라이언스 만기·한도", _FIN),
        ("/regulatory/notices", "공시·AI 색인", _FIN),
        ("/regulatory/codes", "코드 마스터(약가·수가·재료)", _ALL),
        ("/regulatory/drug-map", "약품 코드 매핑", _ALL),
        ("/regulatory/fees", "수가 조회", _ALL),
        ("/regulatory/nursing-grade", "간호등급 모니터", _MGR),
        ("/regulatory/safety", "환자안전사고 보고", _EVERYONE),   # 자율보고=전 직원(처리는 서버 게이트)
        ("/regulatory/radiation", "방사선 안전관리", _MGR),   # 기록=ADMIN·조회 매니저+
        ("/regulatory/infection", "감염관리 지표", _MGR),   # 인증평가·입력=감염관리실/관리자
        ("/regulatory/accreditation", "인증평가 준비", _MGR)]),   # 조사항목 상시준비·품질팀
    ("재고·조달", [
        ("/inventory-requisitions", "구매요청(PR→발주)", _EVERYONE),   # 부서 수요 기안(전 직원)·승인/전환은 서버 게이트
        ("/inventory-material-requests", "자재청구(불출)", _EVERYONE),   # 병동/부서 청구(전 직원)·승인/불출은 서버 게이트
        # 출입증 발급·목록·입고가 scm.items write — DIRECTOR는 write가 없어 403(2026-10-09 정합)
        ("/inventory/workstation", "재고 워크스테이션", _LOGM - {"DIRECTOR"}),
        ("/inventory", "품목·마약류·발주", _LOGM),
        ("/inventory-valuation", "재고 평가·안전재고", _LOGM),
        ("/inventory-stocktake", "재고 실사", _LOGM),
        ("/inventory-consignment", "위탁재고(임플란트·스텐트)", _LOGM),
        ("/inventory-returns", "입고 반품(RMA)", _LOGM),
        ("/inventory-contracts", "위수탁 계약", _LOGM),
        ("/inventory-vendors", "거래처(공급사) 관리", _LOGM),
        ("/inventory-rfq", "소싱(RFQ·견적·단가계약)", _LOGM),
        ("/inventory-recalls", "의약품 회수·의료폐기물", _LOGM),
        ("/inventory-replenishment", "안전재고 자동발주(보충)", _LOGM)]),
    ("IT자산", [   # IA — ITAM 독립(구 재고에서 분리)
        ("/assets/it", "IT 자산관리", _IT),
        ("/assets/services", "SW·클라우드·구독", _IT),
        ("/assets/cost", "IT 비용 점검", _IT),
        ("/assets/offboarding", "IT 퇴사 회수", _IT)]),
    # IA — 과밀 재무(18항목)를 원장·결산 / 자금관리 / 고정자산·리스로 3분할(발견성·기획서 §2)
    ("재무·회계", [
        ("/finance", "전표·예산", _FINM),
        ("/finance-budgets", "예산 관리(편성·전용·이월·약정)", _FIN),   # 편성/변경 write=DIRECTOR/ADMIN
        ("/finance-accounts", "계정과목 관리", _FINM),
        ("/finance-statements", "결산·재무제표", _FINM),
        ("/finance-ifrs", "IFRS 정합 현황", _FINM),
        ("/finance-periods", "기간 마감(월결산)", _FINM),
        ("/finance-ancillary", "부대사업 수익", _FINM),
        ("/finance-audit", "감사·이상탐지", _FINM)]),
    ("자금관리", [
        ("/finance-settlement", "채권·채무·자금", _FINM),
        ("/finance-disbursements", "AP 지급결의(이체배치)", _FIN),   # 결의·승인·실행=ADMIN(finance.settlement)
        ("/finance-aging-party", "거래처별 연령분석", _FINM),
        ("/finance-corporate-accounts", "법인 자금계좌", _FINM),
        ("/finance-daily-cash", "자금일보(일일 시재)", _FINM),
        ("/finance-cash-forecast", "자금수지 예측", _FINM),
        ("/finance-loans", "차입금 관리", _FIN)]),   # 차입·이자·상환(write=ADMIN·finance.periods)
    ("고정자산·리스", [
        ("/assets", "고정자산", _FINM),
        ("/finance-asset-inventory", "고정자산 실사(재물조사)", _FINM),
        ("/finance-leases", "리스(K-IFRS16)", _FINM)]),
    ("원가·수익성", [   # IA — 원가·수익성 독립(구 재무에서 분리)
        ("/costing", "원가 분석", _FINM),
        ("/costing-abc", "ABC 원가배부", _FINM),
        ("/costing-pl", "진료과 손익", _FINM),
        ("/finance-simulation", "재무 시뮬레이션", _FINM),
        ("/finance-voucher-status", "검진권 현황", _FINM),
        ("/finance-voucher-rules", "검진권 전기규칙", _FINM)]),
    ("시스템", [
        ("/tools/spreadsheet", "스프레드시트", _FIN),
        ("/admin/users", "사용자 권한", _FIN),
        ("/admin/menu", "메뉴 권한", _FIN),
        ("/admin/org", "기관 설정", _FIN),
        ("/admin/integrations", "연동 관리", _FIN),
        # 2026-10-09 시나리오 3회차: 인박스 정체건 재처리·처리 완료(사유) — 조치는 관리자 전용(admin.staging write)
        ("/admin/inbound", "연동 인박스", frozenset({"ADMIN"})),
        ("/admin/logs", "감사·접속기록", _FIN),
        # 도메인 경계 불변식 상시 점검 — 손으로 SQL 돌려 찾던 정합 문제를 기능화
        ("/admin/consistency", "운영 정합성 점검", _FIN),
        ("/admin/schedule", "일정 캘린더", _FIN),       # 정기업무·법정기한·공휴일·이의신청 한 달 보기(v1.302)
        ("/admin/batch-jobs", "배치 작업", _FIN),      # worker-batch 잡 일정·이력·지금 실행·일시정지(v1.302)
        ("/admin/decisions", "결정·조치함", _FIN),   # 사람 결정 항목(0226)+화면 조치 분류별(2026-10-11)
        ("/admin/recurring-tasks", "정기업무", _FIN)]),   # 주/월/분기 배치·반복업무(0244)
]


# 메뉴 항목 → 그 화면의 **주 데이터** 권한(resource, action). 메뉴에 노출되는 모든 역할은 이 권한이
# NONE이 아니어야 한다(test_scenario_menu_policy가 강제). 메뉴 노출≠권한이지만, 주 데이터를 거부당하는
# 화면을 메뉴로 보여주면 빈 화면/403만 남는다(2026-10-09 프런트 시나리오 크롤 발견). 화면의 보조 패널
# (예: 재고평가의 저가법=finance.periods)은 화면이 역할별로 숨긴다. 신규·수정 항목은 여기에 등록할 것.
MENU_READ_SCOPES: dict[str, tuple[str, str]] = {
    "/patients/closing": ("patient.closing", "read"),
    "/patients/reservations": ("patient.patients", "read"),
    "/patients": ("patient.patients", "read"),
    "/patients/receivables": ("patient.closing", "read"),
    "/patients/voc": ("patient.voc", "write"),          # 접수=전 직원(조회는 SELF 행스코프)
    "/patients/referrals": ("patient.referral", "read"),
    "/hr/medical-contracts": ("hr.payroll", "read"),
    "/hr/employment-contracts": ("hr.payroll", "read"),
    "/hr/incentive": ("hr.payroll", "read"),
    "/hr/statutory": ("hr.payroll", "read"),
    "/hr/retirement": ("hr.payroll", "read"),
    "/hr/recruitment": ("hr.recruit", "read"),
    "/regulatory/radiation": ("regulatory.master", "read"),
    "/regulatory/safety": ("regulatory.safety", "write"),
    "/regulatory/infection": ("regulatory.infection", "read"),
    "/regulatory/accreditation": ("regulatory.accreditation", "read"),
    "/compliance": ("regulatory.compliance", "read"),
    "/inventory/workstation": ("scm.items", "write"),
    "/inventory-valuation": ("scm.items", "read"),
    "/inventory-material-requests": ("scm.requisition", "write"),
    "/inventory-requisitions": ("scm.requisition", "write"),
    "/finance": ("finance.vouchers", "read"),
    "/finance-budgets": ("finance.budgets", "read"),
    "/admin/decisions": ("admin.decisions", "read"),
    "/admin/schedule": ("admin.recurring", "read"),
    "/admin/batch-jobs": ("admin.recurring", "read"),
    "/admin/inbound": ("admin.staging", "read"),
    "/admin/recurring-tasks": ("admin.recurring", "read"),
}

class MenuService:
    async def _overrides(self, session: AsyncSession) -> dict[tuple[str, str], bool]:
        rows = (await session.execute(select(MenuPermission))).scalars().all()
        return {(r.menu_key, r.role): r.visible for r in rows}

    def _visible(self, ov: dict, key: str, default_roles: frozenset, role: str) -> bool:
        return ov.get((key, role), role in default_roles)

    # 비활성 불가 — 이걸 끄면 관리자가 되돌릴 화면 자체를 잃는다(락아웃 방지).
    UNDISABLEABLE = frozenset({"/admin/menu", "/admin/org", "/dashboard"})

    async def _disabled_keys(self, session: AsyncSession) -> set[str]:
        """기관 단위로 숨긴 메뉴(그룹웨어 중복 등) — 역할 무관 전역."""
        from erp.common.models import OrgSettings
        row = await session.scalar(select(OrgSettings).limit(1))
        keys = set(row.disabled_menu_keys or []) if row is not None else set()
        return keys - self.UNDISABLEABLE

    async def menu_for(self, session: AsyncSession, *, role: str) -> dict:
        """현재 역할이 볼 메뉴(그룹·항목) — 프론트 렌더용. 빈 그룹은 생략."""
        ov = await self._overrides(session)
        off = await self._disabled_keys(session)
        groups = []
        for group, items in MENU_CATALOG:
            visible = [{"href": h, "label": lbl} for h, lbl, dr in items
                       if h not in off and self._visible(ov, h, dr, role)]
            if visible:
                groups.append({"group": group, "items": visible})
        return {"groups": groups}

    async def matrix(self, session: AsyncSession) -> dict:
        """관리자 편집용 매트릭스 — 항목별 역할 노출 현황(기본+오버라이드 반영)."""
        ov = await self._overrides(session)
        off = await self._disabled_keys(session)
        items = []
        for group, entries in MENU_CATALOG:
            for h, lbl, dr in entries:
                items.append({"menu_key": h, "label": lbl, "group": group,
                              "org_disabled": h in off,
                              "undisableable": h in self.UNDISABLEABLE,
                              "visible": {role: self._visible(ov, h, dr, role)
                                          for role in ROLES}})
        return {"roles": ROLES, "items": items}

    async def set_org_disabled(self, session: AsyncSession, *, menu_keys: list[str],
                               actor: int | None) -> list[str]:
        """기관 단위 메뉴 비활성 목록 저장(전량 치환) — 역할 무관.

        그룹웨어 전자결재가 별도로 있는 기관에서 ERP 셀프서비스 화면이 중복 입구가 되는 것을
        관리자가 한 번에 끄기 위한 스위치. 락아웃 방지를 위해 UNDISABLEABLE은 저장 시 제외한다.
        """
        from erp.common.models import OrgSettings
        valid = {h for _g, es in MENU_CATALOG for h, _l, _d in es}
        keys = sorted({k for k in menu_keys if k in valid} - self.UNDISABLEABLE)
        row = await session.scalar(select(OrgSettings).limit(1))
        if row is None:
            row = OrgSettings(name="(미설정)")
            session.add(row)
        row.disabled_menu_keys = keys
        row.updated_by = actor
        await session.flush()
        return keys

    async def set_overrides(self, session: AsyncSession, *, items: list[dict],
                            actor: int | None) -> int:
        """오버라이드 일괄 저장 — (menu_key, role) upsert. 반환: 반영 건수."""
        valid_keys = {h for _g, es in MENU_CATALOG for h, _l, _d in es}
        ov = {(r.menu_key, r.role): r for r in
              (await session.execute(select(MenuPermission))).scalars().all()}
        n = 0
        for it in items:
            key, role, visible = it.get("menu_key"), it.get("role"), it.get("visible")
            if key not in valid_keys or role not in ROLES or not isinstance(visible, bool):
                continue
            row = ov.get((key, role))
            if row is None:
                session.add(MenuPermission(menu_key=key, role=role, visible=visible,
                                           updated_by=actor))
            else:
                row.visible, row.updated_by = visible, actor
            n += 1
        return n
