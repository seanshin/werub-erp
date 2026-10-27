"""역할별 메뉴 노출 — 카탈로그 기본값 + DB 오버라이드."""
from erp.common.menu_service import MenuService

svc = MenuService()


def _hrefs(menu: dict) -> set[str]:
    return {it["href"] for g in menu["groups"] for it in g["items"]}


def test_catalog_no_duplicate_href():
    """카탈로그 내 href 중복 금지 — 항목은 한 그룹에만(이동 시 원그룹 제거 누락 방지)."""
    from erp.common.menu_service import MENU_CATALOG
    hrefs = [h for _g, items in MENU_CATALOG for h, _lbl, _r in items]
    assert len(hrefs) == len(set(hrefs)), \
        f"중복 href: {[h for h in hrefs if hrefs.count(h) > 1]}"


async def test_personal_group(session):
    """개인 기능 그룹 — 전 직원이 '자기 것'을 보고 신청하는 화면만(기획서 §2)."""
    menu = await svc.menu_for(session, role="STAFF")
    my = next((g for g in menu["groups"] if g["group"] == "개인 기능"), None)
    assert my is not None, "개인 기능 그룹 누락"
    keys = {it["href"] for it in my["items"]}
    for k in ("/hr/payroll", "/finance-expenses", "/hr/attendance", "/hr/leaves",
              "/hr/certificates", "/hr/evaluation", "/hr/welfare", "/assets/my", "/security"):
        assert k in keys, f"개인 기능에 {k} 누락"
    # 경계 — 자금결재는 개인 셀프서비스가 아니라 업무 기능이라 별도 그룹(사용자 결정)
    assert not (keys & {"/approvals", "/approvals-drafts", "/approvals-lines"}), \
        "개인 기능에 자금결재(업무 기능)가 섞임"


async def test_fund_approval_group(session):
    """자금결재 그룹 — 결재함·기안·결재선 묶음. 역할은 분할 전과 동일해야 한다."""
    menu = await svc.menu_for(session, role="STAFF")
    grp = next((g for g in menu["groups"] if g["group"] == "자금결재"), None)
    assert grp is not None, "자금결재 그룹 누락"
    keys = {it["href"] for it in grp["items"]}
    # 기안·결재선은 전 직원, 결재함은 결재권자만 → STAFF에는 결재함이 보이지 않는다
    assert {"/approvals-drafts", "/approvals-lines"} <= keys
    assert "/approvals" not in keys


async def test_default_role_visibility(session):
    staff = _hrefs(await svc.menu_for(session, role="STAFF"))
    admin = _hrefs(await svc.menu_for(session, role="ADMIN"))
    assert "/dashboard" in staff                               # _EVERYONE 기본
    assert "/patients" not in staff                            # patient.patients read 없음(2026-10-09 정합)
    assert "/finance-expenses" in staff                        # 경비청구는 전 직원(_EVERYONE)
    assert "/approvals-drafts" in staff                        # 기안문서도 전 직원(_EVERYONE)
    assert "/approvals-lines" in staff                         # 결재선 관리도 전 직원(_EVERYONE)
    assert "/inventory-requisitions" in staff                  # 구매요청 기안은 전 직원(_EVERYONE)
    assert "/inventory-material-requests" in staff             # 자재청구도 전 직원(_EVERYONE)
    assert "/patients/voc" in staff                            # 민원 접수는 전 직원(_EVERYONE)
    assert "/regulatory/safety" in staff                       # 환자안전 자율보고는 전 직원(_EVERYONE)
    assert "/patients/referrals" not in staff                  # 진료의뢰·회송 PII 관리한정(_MGR)
    assert "/admin/decisions" in admin and "/admin/decisions" not in staff   # 결정 대기함 _FIN
    assert "/admin/recurring-tasks" in admin and "/admin/recurring-tasks" not in staff  # 정기업무 _FIN
    mgr = _hrefs(await svc.menu_for(session, role="MANAGER"))
    assert "/patients/referrals" in mgr                                       # 진료의뢰·회송 _MGR(PII 한정)
    assert "/hr/training" in mgr and "/hr/training" not in staff              # 원내 교육이수 _MGR
    # 일반직 근로계약 = 보수(hr.payroll) — MANAGER 권한 없음 → _FIN(2026-10-09 메뉴·권한 정합)
    assert "/hr/employment-contracts" in admin and "/hr/employment-contracts" not in mgr
    assert "/hr/employment-contracts" not in staff
    assert "/hr/personnel-records" in admin and "/hr/personnel-records" not in staff  # 직급호봉·상벌 _FIN
    assert "/hr/recruitment" in mgr and "/hr/recruitment" not in staff        # 채용 ATS _MGR(PII)
    assert "/hr/evaluation" in staff                                          # 인사평가(자기평가·전직원)
    assert "/hr/welfare" in staff                                             # 복리후생(신청·전직원)
    assert "/finance" not in staff                             # FIN 전용 — STAFF 미노출
    assert "/patients/receivables" not in staff                # 미수금 채권관리 _FIN(patient.closing)
    assert "/finance" in admin and "/admin/users" in admin     # ADMIN 노출
    assert "/patients/receivables" in admin                    # 미수금 채권관리 ADMIN 노출
    assert "/finance-budgets" in admin and "/finance-budgets" not in staff   # 예산 관리 _FIN
    assert "/finance-loans" in admin and "/finance-loans" not in staff       # 차입금 관리 _FIN
    assert "/finance-disbursements" in admin and "/finance-disbursements" not in staff  # 지급결의 _FIN
    assert "/regulatory/radiation" in mgr and "/regulatory/radiation" not in staff  # 방사선 _MGR
    assert "/regulatory/infection" in mgr and "/regulatory/infection" not in staff  # 감염관리 _MGR
    assert "/regulatory/accreditation" in mgr and "/regulatory/accreditation" not in staff  # 인증평가 _MGR
    # 신규 FE 화면(자금일보) — _FINM 노출·STAFF 미노출
    finm = _hrefs(await svc.menu_for(session, role="FINANCE_MANAGER"))
    assert "/finance-daily-cash" in finm and "/finance-daily-cash" not in staff
    assert "/finance-aging-party" in finm and "/finance-aging-party" not in staff
    assert "/finance-ancillary" in finm and "/finance-ancillary" not in staff
    assert "/finance-asset-inventory" in finm and "/finance-asset-inventory" not in staff
    logm = _hrefs(await svc.menu_for(session, role="LOGISTICS_MANAGER"))
    assert "/inventory-consignment" in logm and "/inventory-consignment" not in staff
    assert "/inventory-returns" in logm and "/inventory-returns" not in staff
    assert "/inventory-vendors" in logm and "/inventory-vendors" not in staff
    assert "/inventory-rfq" in logm and "/inventory-rfq" not in staff       # 소싱 _LOGM
    assert "/inventory-replenishment" in logm and "/inventory-replenishment" not in staff  # 보충 _LOGM
    assert "/inventory-recalls" in logm and "/inventory-recalls" not in staff  # 회수·폐기물 _LOGM


async def test_domain_managers_see_personal_menus(session):
    """전체검토 X0-8 — 도메인 관리자(재무·IT·물류)도 본인 급여명세·2FA·대시보드 등 개인 메뉴 노출."""
    for role in ("FINANCE_MANAGER", "IT_MANAGER", "LOGISTICS_MANAGER"):
        hrefs = _hrefs(await svc.menu_for(session, role=role))
        assert {"/dashboard", "/security", "/hr/payroll", "/hr/leaves",
                "/hr/certificates", "/assets/my"} <= hrefs, (role, hrefs)
    # 고아 화면 연결 — 급여 설정은 _FIN(관리자)만
    admin = _hrefs(await svc.menu_for(session, role="ADMIN"))
    assert "/admin/payroll-settings" in admin and "/assets/my" in admin
    # 원무(수납·일마감)는 도메인 관리자에게 열지 않음(개인 메뉴만 확장)
    finm = _hrefs(await svc.menu_for(session, role="FINANCE_MANAGER"))
    assert "/patients" not in finm and "/patients/closing" not in finm


async def test_override_shows_hidden_menu(session):
    n = await svc.set_overrides(session, items=[
        {"menu_key": "/finance", "role": "STAFF", "visible": True}], actor=1)
    assert n == 1
    staff = _hrefs(await svc.menu_for(session, role="STAFF"))
    assert "/finance" in staff                                 # 오버라이드로 노출


async def test_override_hides_default_menu(session):
    await svc.set_overrides(session, items=[
        {"menu_key": "/patients/voc", "role": "STAFF", "visible": False}], actor=1)
    staff = _hrefs(await svc.menu_for(session, role="STAFF"))
    assert "/patients/voc" not in staff                        # 기본 노출을 숨김


async def test_matrix_and_invalid_ignored(session):
    m = await svc.matrix(session)
    assert m["roles"] == ["DIRECTOR", "ADMIN", "MANAGER", "STAFF",
                          "IT_MANAGER", "FINANCE_MANAGER", "LOGISTICS_MANAGER"]
    fin = next(i for i in m["items"] if i["menu_key"] == "/finance")
    assert fin["visible"]["ADMIN"] is True and fin["visible"]["STAFF"] is False
    # 도메인 관리자 메뉴 노출 — 재무 메뉴는 회계관리자만(IT/물류 관리자 제외)
    assert fin["visible"]["FINANCE_MANAGER"] is True
    assert fin["visible"]["IT_MANAGER"] is False and fin["visible"]["LOGISTICS_MANAGER"] is False
    it = next(i for i in m["items"] if i["menu_key"] == "/assets/it")
    assert it["visible"]["IT_MANAGER"] is True and it["visible"]["FINANCE_MANAGER"] is False
    # 잘못된 키·역할은 무시
    n = await svc.set_overrides(session, items=[
        {"menu_key": "/nope", "role": "STAFF", "visible": True},
        {"menu_key": "/finance", "role": "BOGUS", "visible": True},
        {"menu_key": "/finance", "role": "STAFF", "visible": "yes"}], actor=1)
    assert n == 0


# ---- 기관 단위 메뉴 비활성(그룹웨어 중복 기능 숨김·마이그 0245) ----

async def test_org_disable_hides_from_every_role(session):
    """역할 무관 전역 숨김 — MenuPermission은 역할 7개를 다 꺼야 해 하나만 놓쳐도 노출됐다."""
    await svc.set_org_disabled(session, menu_keys=["/hr/leaves"], actor=1)
    for role in ("STAFF", "MANAGER", "ADMIN", "DIRECTOR", "FINANCE_MANAGER"):
        assert "/hr/leaves" not in _hrefs(await svc.menu_for(session, role=role)), role
    # 다른 개인 기능은 그대로
    assert "/hr/welfare" in _hrefs(await svc.menu_for(session, role="STAFF"))


async def test_org_disable_is_replace_not_append(session):
    """전량 치환 — 재저장 시 이전 목록이 남으면 되돌릴 수 없다."""
    await svc.set_org_disabled(session, menu_keys=["/hr/leaves", "/hr/welfare"], actor=1)
    await svc.set_org_disabled(session, menu_keys=["/hr/welfare"], actor=1)
    hrefs = _hrefs(await svc.menu_for(session, role="STAFF"))
    assert "/hr/leaves" in hrefs and "/hr/welfare" not in hrefs


async def test_org_disable_cannot_lock_admin_out(session):
    """락아웃 방지 — 되돌릴 화면(/admin/menu·/admin/org)과 홈은 끌 수 없다."""
    kept = await svc.set_org_disabled(
        session, menu_keys=["/admin/menu", "/admin/org", "/dashboard", "/hr/leaves"], actor=1)
    assert kept == ["/hr/leaves"]
    admin = _hrefs(await svc.menu_for(session, role="ADMIN"))
    assert "/admin/menu" in admin and "/dashboard" in admin


async def test_org_disable_ignores_unknown_key(session):
    """카탈로그에 없는 키는 저장하지 않는다(오타·구 경로 잔재 방지)."""
    assert await svc.set_org_disabled(session, menu_keys=["/no/such/page"], actor=1) == []


async def test_matrix_exposes_org_disabled_flags(session):
    """관리화면이 '기관 비활성'과 '비활성 불가'를 구분해 보여줄 수 있어야 한다."""
    await svc.set_org_disabled(session, menu_keys=["/hr/leaves"], actor=1)
    m = await svc.matrix(session)
    by = {i["menu_key"]: i for i in m["items"]}
    assert by["/hr/leaves"]["org_disabled"] is True
    assert by["/admin/menu"]["undisableable"] is True
    assert by["/hr/welfare"]["org_disabled"] is False


async def test_org_disable_endpoint_registered_and_rbac(session, client):
    """HTTP 계층 — 라우터 등록·RBAC·응답. 서비스 직호출만으로는 미등록 EP를 못 잡는다."""
    from tests._http import auth_as
    admin = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.put("/api/v1/admin/menu-org-disabled", headers=admin,
                         json={"menu_keys": ["/hr/leaves", "/admin/menu"]})
    assert r.status_code == 200, r.text
    assert r.json()["disabled_menu_keys"] == ["/hr/leaves"]   # 락아웃 키는 서버가 탈락

    m = await client.get("/api/v1/admin/menu-permissions", headers=admin)
    by = {i["menu_key"]: i for i in m.json()["items"]}
    assert by["/hr/leaves"]["org_disabled"] is True

    # 일반 직원은 저장 불가 + 메뉴에서 사라진 것을 본인 /menu로 확인
    staff = await auth_as(session, user_id=2, role="STAFF")
    assert (await client.put("/api/v1/admin/menu-org-disabled", headers=staff,
                             json={"menu_keys": []})).status_code == 403
    mine = await client.get("/api/v1/menu", headers=staff)
    hrefs = [i["href"] for g in mine.json()["groups"] for i in g["items"]]
    assert "/hr/leaves" not in hrefs and "/hr/welfare" in hrefs
