"""ITAM(IT 자산관리) M1 — 대장·배정·반납·QR 조회·벤더 계약·RBAC."""
from datetime import date

import pytest

from erp.common.router import CurrentUser
from erp.errors import ForbiddenError, ValidationError
from erp.hr.models import Employee
from erp.itam.router import (
    AssetBody, AssignBody, ContractBody, ReturnBody, assign_asset, create_asset,
    create_contract, get_asset, list_assets, lookup_asset, patch_asset, return_asset,
)
from erp.itam.service import ITAMService

_ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
_STAFF = CurrentUser(user_id=2, role="STAFF", dept_id=1, license_flag=True)
svc = ITAMService()


def _emp(no: str, name: str) -> Employee:
    return Employee(employee_no=no, name=name, hired_on=date(2025, 1, 1),
                    job_group="STAFF", employment_type="REGULAR", source="ERP")


async def test_asset_lifecycle_and_qr_lookup(session):
    emp = _emp("E-IT1", "김IT")
    session.add(emp)
    await session.flush()

    # 등록 (PHI·네트워크 필드 포함)
    a = await create_asset(AssetBody(asset_tag="IT-0001", name="개발용 노트북", category="LAPTOP",
                                     serial_no="SN-777", handles_phi=True, ip_addr="10.0.0.5",
                                     hostname="dev-nb-01"), user=_ADMIN, session=session)
    assert a["status"] == "IN_STOCK" and a["handles_phi"] is True

    # QR/바코드 스캔 조회
    look = await lookup_asset(tag="IT-0001", user=_ADMIN, session=session)
    assert look["found"] and look["asset"]["name"] == "개발용 노트북"
    miss = await lookup_asset(tag="NOPE", user=_ADMIN, session=session)
    assert miss["found"] is False

    # 배정 → ASSIGNED·현재 사용자 세팅
    await assign_asset(a["id"], AssignBody(employee_id=emp.id), user=_ADMIN, session=session)
    got = await get_asset(a["id"], user=_ADMIN, session=session)
    assert got["status"] == "ASSIGNED" and got["assigned_employee_id"] == emp.id
    assert len(got["assignments"]) == 1 and got["assignments"][0]["returned_on"] is None

    # 반납 → IN_STOCK·이력 close
    await return_asset(a["id"], ReturnBody(condition="정상"), user=_ADMIN, session=session)
    got2 = await get_asset(a["id"], user=_ADMIN, session=session)
    assert got2["status"] == "IN_STOCK" and got2["assigned_employee_id"] is None
    assert got2["assignments"][0]["returned_on"] is not None

    # 분실 처리 후 배정 거부
    await patch_asset(a["id"], _patch(status="LOST"), user=_ADMIN, session=session)
    with pytest.raises(ValidationError):
        await assign_asset(a["id"], AssignBody(employee_id=emp.id), user=_ADMIN, session=session)


async def test_duplicate_tag_and_serial_rejected(session):
    await create_asset(AssetBody(asset_tag="IT-DUP", name="A", serial_no="S1"),
                       user=_ADMIN, session=session)
    with pytest.raises(ValidationError):     # 태그 중복
        await create_asset(AssetBody(asset_tag="IT-DUP", name="B"), user=_ADMIN, session=session)
    with pytest.raises(ValidationError):     # 시리얼 중복
        await create_asset(AssetBody(asset_tag="IT-OK", name="C", serial_no="S1"),
                           user=_ADMIN, session=session)


async def test_contract_and_asset_link(session):
    c = await create_contract(ContractBody(vendor="델", title="노트북 유지보수", kind="AMC",
                                            end_on=date(2027, 1, 1), amount=1_200_000),
                              user=_ADMIN, session=session)
    a = await create_asset(AssetBody(asset_tag="IT-CT", name="서버", category="SERVER",
                                     contract_id=c["id"]), user=_ADMIN, session=session)
    assert a["contract_id"] == c["id"]


async def test_rbac_staff_denied(session):
    with pytest.raises(ForbiddenError):
        await create_asset(AssetBody(asset_tag="X", name="Y"), user=_STAFF, session=session)
    with pytest.raises(ForbiddenError):
        await list_assets(user=_STAFF, session=session)


async def test_list_filter_and_counts(session):
    await create_asset(AssetBody(asset_tag="L1", name="노트북1", category="LAPTOP"),
                       user=_ADMIN, session=session)
    await create_asset(AssetBody(asset_tag="S1", name="서버1", category="SERVER",
                                 status="REPAIR"), user=_ADMIN, session=session)
    out = await list_assets(category="SERVER", user=_ADMIN, session=session)
    assert len(out["items"]) == 1 and out["items"][0]["asset_tag"] == "S1"
    assert out["counts"].get("IN_STOCK") == 1 and out["counts"].get("REPAIR") == 1


async def test_bulk_upsert_create_update_skip(session):
    from erp.itam.router import BulkAssetBody, BulkAssetRow, bulk_upsert
    # 선등록 1건(갱신 대상)
    await create_asset(AssetBody(asset_tag="B-1", name="구형", category="DESKTOP"),
                       user=_ADMIN, session=session)
    out = await bulk_upsert(BulkAssetBody(rows=[
        BulkAssetRow(asset_tag="B-1", name="갱신된노트북", category="LAPTOP", handles_phi=True),  # 갱신
        BulkAssetRow(asset_tag="B-2", name="신규서버", category="SERVER"),                        # 신규
        BulkAssetRow(asset_tag="", name="무시"),                                                  # 건너뜀
        BulkAssetRow(asset_tag="B-3"),                                                            # 이름없음 건너뜀
    ]), user=_ADMIN, session=session)
    assert out["created"] == 1 and out["updated"] == 1 and out["skipped"] == 2
    a = await svc.lookup_by_tag(session, "B-1")
    assert a.name == "갱신된노트북" and a.category == "LAPTOP" and a.handles_phi is True

    # RBAC — STAFF 거부
    with pytest.raises(ForbiddenError):
        await bulk_upsert(BulkAssetBody(rows=[]), user=_STAFF, session=session)


async def test_phi_and_assigned_filters(session):
    from erp.itam.router import list_assets as list_ep
    emp = _emp("E-F", "필터")
    session.add(emp)
    await session.flush()
    await create_asset(AssetBody(asset_tag="F-PHI", name="PHI기기", handles_phi=True),
                       user=_ADMIN, session=session)
    a2 = await create_asset(AssetBody(asset_tag="F-N", name="일반"), user=_ADMIN, session=session)
    await assign_asset(a2["id"], AssignBody(employee_id=emp.id), user=_ADMIN, session=session)
    phi = await list_ep(handles_phi=True, user=_ADMIN, session=session)
    assert [x["asset_tag"] for x in phi["items"]] == ["F-PHI"]
    unassigned = await list_ep(assigned=False, user=_ADMIN, session=session)
    assert "F-PHI" in [x["asset_tag"] for x in unassigned["items"]]
    assert "F-N" not in [x["asset_tag"] for x in unassigned["items"]]


def _patch(**kw):
    from erp.itam.router import AssetPatch
    return AssetPatch(**kw)


async def test_software_service_seat_compliance(session):
    from erp.itam.router import (AllocBody, ServiceBody, allocate_seat, create_service,
                                 get_service, list_services)
    e1, e2, e3 = _emp("SW1", "가"), _emp("SW2", "나"), _emp("SW3", "다")
    session.add_all([e1, e2, e3])
    await session.flush()

    s = await create_service(ServiceBody(name="Microsoft 365", service_type="SAAS",
                                         billing_model="SEAT_RECURRING", seats=2, vendor="MS",
                                         license_key="ABCD-1234-KEY99", currency="USD",
                                         recurring_cost=15), user=_ADMIN, session=session)
    assert s["key_masked"] == "****EY99" and s["currency"] == "USD"   # 키 마스킹

    # 좌석 3개 배정 → 보유 2 초과(위반)
    for e in (e1, e2, e3):
        await allocate_seat(s["id"], AllocBody(employee_id=e.id), user=_ADMIN, session=session)
    got = await get_service(s["id"], user=_ADMIN, session=session)
    assert got["compliance"]["seats"] == 2 and got["compliance"]["used"] == 3
    assert got["compliance"]["over"] is True and got["compliance"]["unused"] == 0

    lst = await list_services(service_type="SAAS", user=_ADMIN, session=session)
    assert lst["items"][0]["compliance"]["over"] is True


async def test_service_type_validation_and_rbac(session):
    from erp.itam.router import ServiceBody, create_service
    with pytest.raises(ValidationError):
        await create_service(ServiceBody(name="X", service_type="BOGUS"), user=_ADMIN, session=session)
    with pytest.raises(ForbiddenError):
        await create_service(ServiceBody(name="Y"), user=_STAFF, session=session)


async def test_compliance_includes_itam_sources(session):
    """구독 만기·해지 데드라인·자산 보증·벤더 계약이 컴플라이언스 대시보드에 노출."""
    from datetime import date, timedelta

    from erp.app.compliance_service import ComplianceDeadlineService
    from erp.itam.router import ServiceBody, create_service
    from erp.itam.service import ITAMService
    soon = date.today() + timedelta(days=10)
    await create_service(ServiceBody(name="Slack", service_type="SAAS", vendor="Salesforce",
                                     expires_on=soon, auto_renew=True, cancel_notice_days=30),
                         user=_ADMIN, session=session)
    await ITAMService().create_asset(session, actor=1, asset_tag="W-1", name="보증장비",
                                     warranty_until=soon)
    out = await ComplianceDeadlineService().deadlines(session, as_of=date.today(), horizon_days=60)
    cats = {i["category"] for i in out["items"]}
    assert "SW·서비스 구독" in cats and "구독 해지기한" in cats and "IT 자산 보증" in cats


async def test_cost_ledger_fx_and_variance_review(session):
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, create_service
    cs = CostService()
    s = await create_service(ServiceBody(name="AWS", service_type="CLOUD",
                                         billing_model="USAGE_METERED", currency="USD"),
                             user=_ADMIN, session=session)
    sid = s["id"]
    # 전월 100만, 당월 150만(+50%) → SPIKE. USD 1000×1350 환율=1,350,000 KRW
    await cs.record_cost(session, actor=1, service_id=sid, period="2026-05", amount=1_000_000)
    await cs.record_cost(session, actor=1, service_id=sid, period="2026-06",
                         amount=1000, currency="USD", fx_rate=1350)
    costs = await cs.costs(session, sid)
    assert costs[0].amount_krw == 1_350_000 and costs[0].period == "2026-06"   # 환율 KRW 환산

    out = await cs.run_review(session, actor=1, period="2026-06")
    assert out["flagged"] == 1                                    # +35% ≥ 30% → SPIKE
    revs = await cs.list_reviews(session, period="2026-06")
    rv, _svc = revs[0]
    assert rv.reason == "SPIKE" and rv.status == "FLAGGED" and float(rv.delta_pct) == 35.0

    # 확정 후 재실행은 보존
    await cs.confirm_review(session, actor=1, review_id=rv.id, note="검토완료")
    await cs.run_review(session, actor=1, period="2026-06")
    rv2 = (await cs.list_reviews(session, period="2026-06"))[0][0]
    assert rv2.status == "CONFIRMED"


async def test_cost_review_missing_input_flagged(session):
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, create_service
    cs = CostService()
    await create_service(ServiceBody(name="Notion", service_type="SAAS",
                                     billing_model="SEAT_RECURRING"), user=_ADMIN, session=session)
    out = await cs.run_review(session, actor=1, period="2026-06")   # 비용 미입력
    assert out["missing"] == 1
    revs = await cs.list_reviews(session, period="2026-06", status="FLAGGED")
    assert revs[0][0].reason == "MISSING"


async def test_opex_summary(session):
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, create_service
    cs = CostService()
    a = await create_service(ServiceBody(name="S1", service_type="SAAS",
                                         billing_model="SEAT_RECURRING"), user=_ADMIN, session=session)
    b = await create_service(ServiceBody(name="S2", service_type="CLOUD",
                                         billing_model="USAGE_METERED"), user=_ADMIN, session=session)
    await cs.record_cost(session, actor=1, service_id=a["id"], period="2026-06", amount=500_000)
    await cs.record_cost(session, actor=1, service_id=b["id"], period="2026-06", amount=300_000)
    summ = await cs.opex_summary(session, "2026-06")
    assert summ["total_krw"] == 800_000
    assert summ["by_type"]["SAAS"] == 500_000 and summ["by_type"]["CLOUD"] == 300_000


async def test_maintenance_and_phi_erasure_gate(session):
    from erp.itam.router import (DisposeBody, MaintenanceBody, add_maintenance, dispose_asset,
                                 get_asset)
    # PHI 장비
    phi = await create_asset(AssetBody(asset_tag="M-PHI", name="PHI서버", handles_phi=True),
                             user=_ADMIN, session=session)
    # 유지보수 기록
    await add_maintenance(phi["id"], MaintenanceBody(kind="REPAIR", on_date=date(2026, 6, 1),
                                                     vendor="델", cost=200000), user=_ADMIN, session=session)
    got = await get_asset(phi["id"], user=_ADMIN, session=session)
    assert len(got["maintenance"]) == 1 and got["maintenance"][0]["kind"] == "REPAIR"

    # 소거 미확인 폐기 → 거부(게이트)
    with pytest.raises(ValidationError):
        await dispose_asset(phi["id"], DisposeBody(data_erased=False), user=_ADMIN, session=session)
    # 소거 확인 후 폐기 OK
    out = await dispose_asset(phi["id"], DisposeBody(data_erased=True), user=_ADMIN, session=session)
    assert out["status"] == "DISPOSED" and out["data_erased"] is True and out["disposed_at"]

    # 비PHI 장비는 소거 미확인도 폐기 가능
    normal = await create_asset(AssetBody(asset_tag="M-N", name="일반"), user=_ADMIN, session=session)
    out2 = await dispose_asset(normal["id"], DisposeBody(data_erased=False), user=_ADMIN, session=session)
    assert out2["status"] == "DISPOSED"


async def test_maintenance_kind_validation_and_rbac(session):
    from erp.itam.router import MaintenanceBody, add_maintenance
    a = await create_asset(AssetBody(asset_tag="MK", name="장비"), user=_ADMIN, session=session)
    with pytest.raises(ValidationError):
        await add_maintenance(a["id"], MaintenanceBody(kind="BOGUS", on_date=date(2026, 6, 1)),
                              user=_ADMIN, session=session)
    with pytest.raises(ForbiddenError):
        await add_maintenance(a["id"], MaintenanceBody(kind="REPAIR", on_date=date(2026, 6, 1)),
                              user=_STAFF, session=session)


async def test_offboarding_holdings(session):
    from erp.itam.router import (AllocBody, AssignBody, ServiceBody, allocate_seat, assign_asset,
                                 create_service, holdings, offboarding)
    from erp.itam.service import ITAMService
    active = _emp("OB-A", "재직자")
    quit_emp = _emp("OB-Q", "퇴사자")
    session.add_all([active, quit_emp])
    await session.flush()
    # 재직 중 배정 → 이후 퇴사(현실 순서 — 퇴직자 신규 배정은 가드로 차단됨)
    a = await create_asset(AssetBody(asset_tag="OB-1", name="노트북"), user=_ADMIN, session=session)
    await assign_asset(a["id"], AssignBody(employee_id=quit_emp.id), user=_ADMIN, session=session)
    s = await create_service(ServiceBody(name="Figma", seats=5), user=_ADMIN, session=session)
    await allocate_seat(s["id"], AllocBody(employee_id=quit_emp.id), user=_ADMIN, session=session)
    quit_emp.resigned_on = date(2026, 6, 30)
    await session.flush()
    # 재직자에도 배정(퇴사 목록엔 안 나와야)
    a2 = await create_asset(AssetBody(asset_tag="OB-2", name="키보드"), user=_ADMIN, session=session)
    await assign_asset(a2["id"], AssignBody(employee_id=active.id), user=_ADMIN, session=session)

    ob = await offboarding(user=_ADMIN, session=session)
    assert len(ob["items"]) == 1 and ob["items"][0]["employee_no"] == "OB-Q"
    assert ob["items"][0]["asset_count"] == 1 and ob["items"][0]["seat_count"] == 1

    h = await holdings(quit_emp.id, user=_ADMIN, session=session)
    assert h["assets"][0]["asset_tag"] == "OB-1" and h["seats"][0]["service_name"] == "Figma"

    # 반납+좌석 해제 후 목록에서 사라짐
    await ITAMService().return_asset(session, actor=1, asset_id=a["id"])
    from erp.itam.software_service import SoftwareServiceService
    alloc_id = h["seats"][0]["allocation_id"]
    await SoftwareServiceService().release(session, actor=1, allocation_id=alloc_id)
    ob2 = await offboarding(user=_ADMIN, session=session)
    assert ob2["items"] == []


async def test_compliance_includes_calibration(session):
    from datetime import date, timedelta

    from erp.app.compliance_service import ComplianceDeadlineService
    from erp.itam.service import ITAMService
    soon = date.today() + timedelta(days=15)
    await ITAMService().create_asset(session, actor=1, asset_tag="CAL-1", name="초음파기",
                                     category="MEDICAL_IT", calibration_due=soon, udi="UDI-999")
    out = await ComplianceDeadlineService().deadlines(session, as_of=date.today(), horizon_days=60)
    assert "의료기기 교정" in {i["category"] for i in out["items"]}


async def test_asset_register_with_user_and_schedules(session):
    from datetime import date, timedelta

    from erp.app.compliance_service import ComplianceDeadlineService
    from erp.itam.router import AssetBody, create_asset, employee_picker, get_asset
    emp = _emp("REG-1", "배정직원")
    session.add(emp)
    await session.flush()
    # 직원 선택 목록
    opts = await employee_picker(user=_ADMIN, session=session)
    assert any(o["employee_no"] == "REG-1" for o in opts["items"])

    soon = date.today() + timedelta(days=12)
    a = await create_asset(AssetBody(
        asset_tag="REG-IT-1", name="개발 서버", category="SERVER",
        acquired_on=date(2026, 1, 5), assigned_employee_id=emp.id, location="전산실",
        spec={"cpu": "Xeon", "ram": "64GB", "disk": "2TB"},
        next_inspection_on=soon, next_maintenance_on=soon, return_due_on=soon,
    ), user=_ADMIN, session=session)
    # 등록 시 사용자 배정됨(이력 생성)
    got = await get_asset(a["id"], user=_ADMIN, session=session)
    assert got["assigned_employee_id"] == emp.id and got["status"] == "ASSIGNED"
    assert got["assigned_employee_name"] == "배정직원"                 # 사용자 이름 표기
    assert got["assignments"][0]["employee_name"] == "배정직원"
    assert got["spec"]["ram"] == "64GB" and got["next_inspection_on"]
    assert len(got["assignments"]) == 1
    from erp.itam.router import list_assets as _list
    lst = await _list(user=_ADMIN, session=session)
    assert any(it["assigned_employee_name"] == "배정직원" for it in lst["items"])

    # 정기점검·회수 예정 → 컴플라이언스 대시보드
    out = await ComplianceDeadlineService().deadlines(session, as_of=date.today(), horizon_days=30)
    cats = {i["category"] for i in out["items"]}
    assert "IT 정기점검" in cats and "IT 자산 회수" in cats


async def test_status_transition_guards(session):
    """검토 H-1 — 폐기/배정 상태 patch 우회 차단·유령 배정 정리."""
    from erp.itam.router import DisposeBody, dispose_asset
    emp = _emp("TG-1", "가드직원")
    session.add(emp)
    await session.flush()
    a = await create_asset(AssetBody(asset_tag="TG-A", name="PHI장비", handles_phi=True),
                           user=_ADMIN, session=session)
    # patch로 DISPOSED 진입 차단(소거 게이트 우회 방지)
    with pytest.raises(ValidationError):
        await patch_asset(a["id"], _patch(status="DISPOSED"), user=_ADMIN, session=session)
    # patch로 ASSIGNED 직접 설정 차단
    with pytest.raises(ValidationError):
        await patch_asset(a["id"], _patch(status="ASSIGNED"), user=_ADMIN, session=session)
    # 배정 후 IN_STOCK patch → open 배정 자동 마감·사용자 해제(유령 배정 방지)
    await assign_asset(a["id"], AssignBody(employee_id=emp.id), user=_ADMIN, session=session)
    await patch_asset(a["id"], _patch(status="IN_STOCK"), user=_ADMIN, session=session)
    got = await get_asset(a["id"], user=_ADMIN, session=session)
    assert got["assigned_employee_id"] is None
    assert got["assignments"][0]["returned_on"] is not None
    # 폐기 후 상태 변경 차단(부활 방지)
    await dispose_asset(a["id"], DisposeBody(data_erased=True), user=_ADMIN, session=session)
    with pytest.raises(ValidationError):
        await patch_asset(a["id"], _patch(status="IN_STOCK"), user=_ADMIN, session=session)


async def test_assign_guards_repair_loaner_resigned(session):
    """수리중 배정 차단·LOANER 상태 보존·퇴직자 배정 차단."""
    emp = _emp("TG-2", "배정직원2")
    quit_e = _emp("TG-3", "퇴직자2")
    quit_e.resigned_on = date(2026, 1, 1)
    session.add_all([emp, quit_e])
    await session.flush()
    rep = await create_asset(AssetBody(asset_tag="TG-R", name="수리중", status="REPAIR"),
                             user=_ADMIN, session=session)
    with pytest.raises(ValidationError):
        await assign_asset(rep["id"], AssignBody(employee_id=emp.id), user=_ADMIN, session=session)
    loan = await create_asset(AssetBody(asset_tag="TG-L", name="대여폰", status="LOANER"),
                              user=_ADMIN, session=session)
    await assign_asset(loan["id"], AssignBody(employee_id=emp.id), user=_ADMIN, session=session)
    got = await get_asset(loan["id"], user=_ADMIN, session=session)
    assert got["status"] == "LOANER" and got["assigned_employee_id"] == emp.id   # 풀 표식 유지
    ok = await create_asset(AssetBody(asset_tag="TG-N", name="정상"), user=_ADMIN, session=session)
    with pytest.raises(ValidationError):
        await assign_asset(ok["id"], AssignBody(employee_id=quit_e.id), user=_ADMIN, session=session)


async def test_fx_rate_required_for_foreign_currency(session):
    """검토 H-2 — 외화 비용 fx_rate 필수(1:1 침묵 환산 차단)."""
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, create_service
    s = await create_service(ServiceBody(name="GCP", service_type="CLOUD",
                                         billing_model="USAGE_METERED", currency="USD"),
                             user=_ADMIN, session=session)
    with pytest.raises(ValidationError):
        await CostService().record_cost(session, actor=1, service_id=s["id"],
                                        period="2026-06", amount=1000, currency="USD")


async def test_duplicate_seat_allocation_rejected(session):
    from erp.itam.router import AllocBody, ServiceBody, allocate_seat, create_service
    emp = _emp("TG-4", "좌석직원")
    session.add(emp)
    await session.flush()
    s = await create_service(ServiceBody(name="Zoom", seats=10), user=_ADMIN, session=session)
    await allocate_seat(s["id"], AllocBody(employee_id=emp.id), user=_ADMIN, session=session)
    with pytest.raises(ValidationError):                       # 중복 open 배정 → used 부풀림 차단
        await allocate_seat(s["id"], AllocBody(employee_id=emp.id), user=_ADMIN, session=session)


async def test_perpetual_sw_seat_based_and_review_lifecycle(session):
    """구매SW 좌석 준수 표기 + run_review 생애주기 필터·확정 카운터 제외."""
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, create_service, get_service
    cs = CostService()
    # 구매 SW(ONE_TIME)도 seats 있으면 좌석 준수 대상
    p = await create_service(ServiceBody(name="AutoCAD LT", service_type="PERPETUAL_SW",
                                         billing_model="ONE_TIME", seats=5),
                             user=_ADMIN, session=session)
    got = await get_service(p["id"], user=_ADMIN, session=session)
    assert got["compliance"]["seat_based"] is True
    # 만료 서비스는 점검 제외(영구 MISSING 오탐 방지)
    await create_service(ServiceBody(name="해지된SaaS", service_type="SAAS",
                                     billing_model="SEAT_RECURRING",
                                     expires_on=date(2026, 3, 31)), user=_ADMIN, session=session)
    # 미개시 서비스도 제외
    await create_service(ServiceBody(name="미개시SaaS", service_type="SAAS",
                                     billing_model="SEAT_RECURRING",
                                     started_on=date(2026, 9, 1)), user=_ADMIN, session=session)
    live = await create_service(ServiceBody(name="운영중SaaS", service_type="SAAS",
                                            billing_model="SEAT_RECURRING"),
                                user=_ADMIN, session=session)
    out = await cs.run_review(session, actor=1, period="2026-06")
    assert out["total"] == 1 and out["missing"] == 1           # 운영중만 대상(미입력 플래그)
    # 확정 후 재실행 → 카운터에서 제외(confirmed로 분리)
    rv = (await cs.list_reviews(session, period="2026-06"))[0][0]
    assert rv.service_id == live["id"]
    await cs.confirm_review(session, actor=1, review_id=rv.id)
    out2 = await cs.run_review(session, actor=1, period="2026-06")
    assert out2["missing"] == 0 and out2["confirmed"] == 1


async def test_create_status_whitelist_and_phi_sticky(session):
    """S-1 create 상태 화이트리스트 + S-2 phi_ever sticky 소거 게이트."""
    from erp.itam.router import DisposeBody, dispose_asset
    # 생성 시 ASSIGNED·DISPOSED 차단
    with pytest.raises(ValidationError):
        await create_asset(AssetBody(asset_tag="WL-1", name="x", status="DISPOSED"),
                           user=_ADMIN, session=session)
    with pytest.raises(ValidationError):
        await create_asset(AssetBody(asset_tag="WL-2", name="y", status="ASSIGNED"),
                           user=_ADMIN, session=session)
    # PHI 지정 후 False로 내려도 소거 게이트 유지(phi_ever sticky)
    a = await create_asset(AssetBody(asset_tag="WL-3", name="phi", handles_phi=True),
                           user=_ADMIN, session=session)
    await patch_asset(a["id"], _patch(handles_phi=False), user=_ADMIN, session=session)
    with pytest.raises(ValidationError):                # 소거 미확인 폐기 여전히 차단
        await dispose_asset(a["id"], DisposeBody(data_erased=False), user=_ADMIN, session=session)
    out = await dispose_asset(a["id"], DisposeBody(data_erased=True), user=_ADMIN, session=session)
    assert out["status"] == "DISPOSED"


async def test_service_cancel_excludes_from_review(session):
    """F-2 서비스 해지 → 비용 점검 대상 제외·재개."""
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, cancel_service, create_service, list_services
    s = await create_service(ServiceBody(name="해지대상", service_type="SAAS",
                                         billing_model="SEAT_RECURRING"), user=_ADMIN, session=session)
    out = await CostService().run_review(session, actor=1, period="2026-06")
    assert out["total"] == 1                              # 활성 → 점검 대상
    await cancel_service(s["id"], user=_ADMIN, session=session)
    got = await list_services(status="CANCELLED", user=_ADMIN, session=session)
    assert got["items"][0]["status"] == "CANCELLED"
    out2 = await CostService().run_review(session, actor=1, period="2026-07")
    assert out2["total"] == 0                             # 해지 → 점검 제외
    await cancel_service(s["id"], reactivate=True, user=_ADMIN, session=session)
    out3 = await CostService().run_review(session, actor=1, period="2026-08")
    assert out3["total"] == 1                             # 재개 → 다시 대상


async def test_cost_validation_and_confirm_guard(session):
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, create_service
    cs = CostService()
    s = await create_service(ServiceBody(name="검증SaaS", billing_model="SEAT_RECURRING"),
                             user=_ADMIN, session=session)
    with pytest.raises(ValidationError):                 # S-8 13월
        await cs.record_cost(session, actor=1, service_id=s["id"], period="2026-13", amount=100)
    with pytest.raises(ValidationError):                 # S-8 음수
        await cs.record_cost(session, actor=1, service_id=s["id"], period="2026-06", amount=-5)
    await cs.record_cost(session, actor=1, service_id=s["id"], period="2026-06", amount=100000)
    await cs.run_review(session, actor=1, period="2026-06")
    rv = (await cs.list_reviews(session, period="2026-06"))[0][0]
    await cs.confirm_review(session, actor=1, review_id=rv.id)
    with pytest.raises(ValidationError):                 # S-7 기확인 재확인 거부
        await cs.confirm_review(session, actor=1, review_id=rv.id)


async def test_bulk_row_limit(session):
    from erp.itam.router import BulkAssetBody, BulkAssetRow, bulk_upsert
    rows = [BulkAssetRow(asset_tag=f"OVER-{i}", name="x") for i in range(2001)]
    with pytest.raises(ValidationError):                 # S-4 2000행 상한
        await bulk_upsert(BulkAssetBody(rows=rows), user=_ADMIN, session=session)


# ── R2 편집층 완결(F-4/5/7/8/10) + S-9 첨부 무결성 ─────────────
async def test_self_acknowledge_ownership(session):
    """F-7 본인 수령 확인 — 배정 당사자만·대리/타인 차단·비직원 차단."""
    from erp.common.models import User
    from erp.itam.router import (
        AssignBody, acknowledge_self, assign_asset, create_asset, my_assignments,
    )
    emp = _emp("E-ACK", "본인")
    other = _emp("E-OTH", "타인")
    session.add_all([emp, other])
    await session.flush()
    u_self = User(email="self@x.example", password_hash="x", role="STAFF", employee_id=emp.id)
    u_other = User(email="oth@x.example", password_hash="x", role="STAFF", employee_id=other.id)
    u_none = User(email="none@x.example", password_hash="x", role="STAFF", employee_id=None)
    session.add_all([u_self, u_other, u_none])
    await session.flush()
    a = await create_asset(AssetBody(asset_tag="ACK-1", name="지급 노트북"),
                           user=_ADMIN, session=session)
    asn = await assign_asset(a["id"], AssignBody(employee_id=emp.id), user=_ADMIN, session=session)
    aid = asn["assignment_id"]
    self_user = CurrentUser(user_id=u_self.id, role="STAFF", dept_id=None, license_flag=True)
    other_user = CurrentUser(user_id=u_other.id, role="STAFF", dept_id=None, license_flag=True)
    none_user = CurrentUser(user_id=u_none.id, role="STAFF", dept_id=None, license_flag=True)
    # 본인 배정 목록(수령 확인 전)
    mine = await my_assignments(user=self_user, session=session)
    assert mine["items"][0]["assignment_id"] == aid
    assert mine["items"][0]["acknowledged_at"] is None
    # 타인은 대리 확인 불가
    with pytest.raises(ValidationError):
        await acknowledge_self(aid, user=other_user, session=session)
    # 비직원 계정 차단
    with pytest.raises(ForbiddenError):
        await acknowledge_self(aid, user=none_user, session=session)
    # 본인 확인 성공 → 재확인 거부
    await acknowledge_self(aid, user=self_user, session=session)
    with pytest.raises(ValidationError):
        await acknowledge_self(aid, user=self_user, session=session)


async def test_service_change_history(session):
    """F-8 갱신 이력 — 만기·단가 변경이 감사 기반 이력에 남는다(최신순)."""
    from erp.itam.router import (
        ServiceBody, ServicePatch, create_service, patch_service, service_history,
    )
    s = await create_service(ServiceBody(name="갱신SaaS", billing_model="SEAT_RECURRING",
                                         expires_on=date(2026, 6, 30), recurring_cost=100000),
                             user=_ADMIN, session=session)
    await patch_service(s["id"], ServicePatch(expires_on=date(2027, 6, 30), recurring_cost=120000),
                        user=_ADMIN, session=session)
    hist = await service_history(s["id"], user=_ADMIN, session=session)
    actions = [h["action"] for h in hist["items"]]
    assert "UPDATE" in actions and "CREATE" in actions
    upd = next(h for h in hist["items"] if h["action"] == "UPDATE")
    assert str(upd["before"]["expires_on"]) == "2026-06-30"     # 이전 만기 보존
    assert upd["after"]["recurring_cost"] == 120000


async def test_asset_and_cost_csv_export(session):
    """F-10 CSV 내보내기 — 자산 대장·비용 원장."""
    from erp.itam.cost_service import CostService
    from erp.itam.router import (
        ServiceBody, create_asset, create_service, export_assets, export_cost_ledger,
    )
    await create_asset(AssetBody(asset_tag="CSV-1", name="감사대상 노트북", category="LAPTOP",
                                 handles_phi=True), user=_ADMIN, session=session)
    resp = await export_assets(user=_ADMIN, session=session)
    body = resp.body.decode("utf-8")
    assert "자산태그" in body and "CSV-1" in body and "감사대상 노트북" in body
    s = await create_service(ServiceBody(name="비용SaaS", billing_model="SEAT_RECURRING"),
                             user=_ADMIN, session=session)
    await CostService().record_cost(session, actor=1, service_id=s["id"], period="2026-07",
                                    amount=50000)
    cresp = await export_cost_ledger(period="2026-07", user=_ADMIN, session=session)
    cbody = cresp.body.decode("utf-8")
    assert "비용SaaS" in cbody and "50000" in cbody


async def test_attachment_integrity_guards(session):
    """S-9 첨부 존재검증 등록 + 폐기 자산 소거확인서 삭제 차단."""
    from erp.common.attachment_router import _DELETE_GUARDS, _EXISTENCE_CHECKS
    from erp.itam.attachment_guards import _asset_exists, _guard_disposed_asset
    from erp.itam.router import DisposeBody, create_asset, dispose_asset
    # 도메인 등록 확인(의존성 역전)
    assert set(_EXISTENCE_CHECKS) >= {"it_asset", "software_service", "itam_contract",
                                      "itam_maintenance"}
    assert "it_asset" in _DELETE_GUARDS
    a = await create_asset(AssetBody(asset_tag="ATT-1", name="폐기예정", handles_phi=True),
                           user=_ADMIN, session=session)
    assert await _asset_exists(session, a["id"]) is True
    assert await _asset_exists(session, 999999) is False        # 고아 첨부 방지
    await _guard_disposed_asset(session, a["id"])               # 폐기 전 → 통과
    await dispose_asset(a["id"], DisposeBody(data_erased=True), user=_ADMIN, session=session)
    with pytest.raises(ValidationError):                        # 폐기 후 소거확인서 삭제 차단
        await _guard_disposed_asset(session, a["id"])


# ── R3 결재선 연동(F-3) + 실사 세션(F-9) + 비용 집계(F-6a) ─────
async def test_disposal_approval_gate(session):
    """F-3 폐기 결재 — 금액은 서버(연결 고정자산 장부가)에서 도출(클라이언트 우회 불가·재검토 HIGH)."""
    from datetime import date as _date

    from erp.common.models import ApprovalRule, User
    from erp.finance.models import FixedAsset
    from erp.itam.router import DisposeBody, dispose_asset
    session.add_all([
        User(id=1, email="disposer@x.example", password_hash="x", role="ADMIN"),
        User(id=2, email="approver@x.example", password_hash="x", role="ADMIN"),
        ApprovalRule(doc_type="ITAM_DISPOSAL", min_amount=1000000, max_amount=None,
                     steps=[{"type": "ROLE", "value": "ADMIN"}], active=True, created_by=1)])
    fa = FixedAsset(asset_no="FA-DISP", name="서버", acquired_on=_date(2024, 1, 1),
                    acquisition_cost=5000000, useful_life_years=5, accumulated_depreciation=0)
    session.add(fa)
    await session.flush()
    disposer = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    # 미연결(고정자산 없음) → 서버 도출 금액 0 → 밴드 미달 → 즉시 폐기
    low = await create_asset(AssetBody(asset_tag="DISP-LOW", name="저가폐기"),
                             user=disposer, session=session)
    out = await dispose_asset(low["id"], DisposeBody(data_erased=True), user=disposer, session=session)
    assert out["status"] == "DISPOSED"
    # 고정자산 연결(장부가 500만) → 밴드 매칭 → 결재 상신(클라이언트가 낮출 방법 없음)
    high = await create_asset(AssetBody(asset_tag="DISP-HI", name="고가폐기", finance_asset_id=fa.id),
                              user=disposer, session=session)
    with pytest.raises(ValidationError, match="결재 승인"):
        await dispose_asset(high["id"], DisposeBody(data_erased=True), user=disposer, session=session)
    assert (await svc.disposal_status(session, high["id"]))["status"] == "PENDING"
    # 상신자 본인 승인 불가(직무분리)
    with pytest.raises(Exception):
        await svc.disposal_approval(session, actor=1, asset_id=high["id"], approve=True)
    # 타 관리자 승인 → APPROVED → 재폐기 성공
    r = await svc.disposal_approval(session, actor=2, asset_id=high["id"], approve=True)
    assert r["status"] == "APPROVED"
    out2 = await dispose_asset(high["id"], DisposeBody(data_erased=True), user=disposer, session=session)
    assert out2["status"] == "DISPOSED"


async def test_audit_session_stamp_and_report(session):
    """F-9 실사 — 스탬프로 last_audited_at 기록·미실사 리포트에서 제외."""
    from erp.itam.router import AuditStampBody, audit_report, audit_stamp
    a = await create_asset(AssetBody(asset_tag="AUD-1", name="실사대상"),
                           user=_ADMIN, session=session)
    rep0 = await audit_report(days=180, user=_ADMIN, session=session)
    assert rep0["total_active"] >= 1 and any(i["asset_tag"] == "AUD-1" for i in rep0["items"])
    st = await audit_stamp(a["id"], AuditStampBody(location="3층 서버실"),
                           user=_ADMIN, session=session)
    assert st["last_audited_at"] is not None and st["location"] == "3층 서버실"
    rep1 = await audit_report(days=180, user=_ADMIN, session=session)
    assert not any(i["asset_tag"] == "AUD-1" for i in rep1["items"])   # 방금 실사 → 미실사 목록 제외


async def test_opex_aggregation_by_center_and_vendor(session):
    """F-6a 비용 집계 — 부서(코스트센터)·벤더별."""
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, create_service
    cs = CostService()
    s1 = await create_service(ServiceBody(name="A", vendor="AWS", cost_center="영상의학",
                                          billing_model="USAGE_METERED"), user=_ADMIN, session=session)
    s2 = await create_service(ServiceBody(name="B", vendor="MS", cost_center="영상의학",
                                          billing_model="SEAT_RECURRING"), user=_ADMIN, session=session)
    await cs.record_cost(session, actor=1, service_id=s1["id"], period="2026-07", amount=300000)
    await cs.record_cost(session, actor=1, service_id=s2["id"], period="2026-07", amount=200000)
    out = await cs.opex_summary(session, "2026-07")
    assert out["total_krw"] == 500000
    assert out["by_cost_center"]["영상의학"] == 500000
    assert out["by_vendor"]["AWS"] == 300000 and out["by_vendor"]["MS"] == 200000


async def test_it_budget_variance(session):
    """R4·F-6 IT 예산 — 코스트센터별·전체 예산 대비 실적·초과·미예산 지출 감지."""
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, create_service
    cs = CostService()
    s1 = await create_service(ServiceBody(name="영상SaaS", cost_center="영상의학",
                                          billing_model="SEAT_RECURRING"), user=_ADMIN, session=session)
    s2 = await create_service(ServiceBody(name="검사SaaS", cost_center="진단검사",
                                          billing_model="SEAT_RECURRING"), user=_ADMIN, session=session)
    await cs.record_cost(session, actor=1, service_id=s1["id"], period="2026-07", amount=1200000)
    await cs.record_cost(session, actor=1, service_id=s2["id"], period="2026-07", amount=300000)
    # 영상의학 예산 100만(초과) + 총예산 200만(미달)
    await cs.upsert_budget(session, actor=1, period="2026-07", cost_center="영상의학", amount_krw=1000000)
    await cs.upsert_budget(session, actor=1, period="2026-07", cost_center=None, amount_krw=2000000)
    v = await cs.budget_variance(session, "2026-07")
    assert v["total_budget_krw"] == 2000000 and v["total_actual_krw"] == 1500000
    assert v["total_over"] is False                    # 총액 미달
    img = next(r for r in v["rows"] if r["cost_center"] == "영상의학")
    assert img["over"] is True and img["variance_krw"] == 200000
    unbud = next(r for r in v["rows"] if r["cost_center"] == "진단검사")
    assert unbud.get("unbudgeted") is True and unbud["budget_krw"] is None   # 예산 없이 지출
    # 예산 삭제
    b = (await cs.list_budgets(session, "2026-07"))[0]
    await cs.delete_budget(session, actor=1, budget_id=b.id)
    assert len(await cs.list_budgets(session, "2026-07")) == 1


async def test_vendor_contract_management_fields(session):
    """벤더 계약 확대 — 체결일·상대/내부 담당자·부서·사유·검토시기 관리 + 수정."""
    from datetime import date as _date

    from erp.common.models import Department
    from erp.itam.router import (
        ContractBody, ContractPatch, create_contract, list_contracts, patch_contract,
    )
    dept = Department(id=90, name="정보전략팀")
    emp = _emp("E-CT1", "계약담당")
    session.add_all([dept, emp])
    await session.flush()
    c = await create_contract(ContractBody(
        vendor="한국오라클", title="DB 유지보수 AMC", kind="AMC",
        signed_on=_date(2026, 1, 5), start_on=_date(2026, 2, 1), end_on=_date(2027, 1, 31),
        next_review_on=_date(2026, 11, 1), amount=12000000, counterparty="김벤더",
        counterparty_contact="vendor@oracle.example / 010-0000-0000",
        owner_employee_id=emp.id, department_id=90, reason="기간 만료 갱신",
        note="핵심 DB — 무중단 조건"), user=_ADMIN, session=session)
    lst = await list_contracts(user=_ADMIN, session=session)
    row = next(x for x in lst["items"] if x["id"] == c["id"])
    assert row["signed_on"] == _date(2026, 1, 5) and row["kind"] == "AMC"
    assert row["counterparty"] == "김벤더" and row["owner_employee_name"] == "계약담당"
    assert row["department_name"] == "정보전략팀" and row["reason"] == "기간 만료 갱신"
    assert row["next_review_on"] == _date(2026, 11, 1)
    # 수정 — 만기 연장·담당자 변경
    upd = await patch_contract(c["id"], ContractPatch(end_on=_date(2028, 1, 31), counterparty="이후임"),
                               user=_ADMIN, session=session)
    assert str(upd["end_on"]) == "2028-01-31" and upd["counterparty"] == "이후임"


async def test_suggest_tag_autogenerate(session):
    """QR 자산 태그 자동 생성 — 다음 순번(prefix-NNNNNN)."""
    from erp.itam.router import suggest_tag
    r0 = await suggest_tag(user=_ADMIN, session=session)
    assert r0["tag"] == "IT-000001"                       # 최초
    await create_asset(AssetBody(asset_tag=r0["tag"], name="자동태그1"), user=_ADMIN, session=session)
    await create_asset(AssetBody(asset_tag="IT-000005", name="수동태그"), user=_ADMIN, session=session)
    r1 = await suggest_tag(user=_ADMIN, session=session)
    assert r1["tag"] == "IT-000006"                       # 최대(5)+1
    r2 = await suggest_tag(prefix="srv", user=_ADMIN, session=session)
    assert r2["tag"] == "SRV-000001"                      # prefix 대문자·독립 순번


async def test_asset_note_roundtrip(session):
    """자산 등록 비고(note) 저장·조회 왕복."""
    a = await create_asset(AssetBody(asset_tag="NOTE-1", name="비고자산",
                                     note="구매 경위·특이사항"), user=_ADMIN, session=session)
    assert a["note"] == "구매 경위·특이사항"
    got = await get_asset(a["id"], user=_ADMIN, session=session)
    assert got["note"] == "구매 경위·특이사항"


async def test_sw_service_expanded_fields_and_token(session):
    """SW/클라우드 등록 확대 — 도메인·링크·IT정보·담당자·예상비용/VAT/토큰단위 + 토큰 암호화·마스킹."""
    from erp.itam.router import ServiceBody, create_service, get_service, patch_service
    s = await create_service(ServiceBody(
        name="AWS 프로덕션", service_type="CLOUD", billing_model="USAGE_METERED",
        vendor="AWS", domain="prod.example.io.kr", homepage_url="https://aws.amazon.com",
        tech_info="ap-northeast-2 · EKS · RDS", sales_contact="김영업 010-1234",
        cost_center="정보전략팀", cost_period="MONTHLY", vat_included=False, usage_unit="토큰",
        recurring_cost=500000, api_token="SECRET-TOKEN-ABCD1234"), user=_ADMIN, session=session)
    assert s["domain"] == "prod.example.io.kr" and s["usage_unit"] == "토큰"
    assert s["vat_included"] is False and s["cost_period"] == "MONTHLY"
    assert s["sales_contact"].startswith("김영업")
    # 토큰은 마스킹만 노출(평문 미노출)
    assert s["token_masked"] == "****1234"
    got = await get_service(s["id"], user=_ADMIN, session=session)
    assert got["tech_info"].startswith("ap-northeast-2") and got["token_masked"] == "****1234"
    # 토큰 재설정 → 새 마스킹
    upd = await patch_service(s["id"], _spatch(api_token="NEW-TOKEN-WXYZ9999"),
                              user=_ADMIN, session=session)
    assert upd["token_masked"] == "****9999"


def _spatch(**kw):
    from erp.itam.router import ServicePatch
    return ServicePatch(**kw)


async def test_offboarding_record_and_complete_gate(session):
    """IT 퇴사 회수 기록(#4) — 검토자·인수인계자·비고 + 미회수 남으면 완료 차단."""
    from erp.itam.router import (
        AssignBody, OffboardingBody, OffboardingPatch, assign_asset, complete_offboarding,
        create_asset, create_offboarding, get_offboarding, patch_offboarding, return_asset,
    )
    leaver = _emp("E-LEAVE", "퇴사자")
    reviewer = _emp("E-REV", "검토자")
    handover = _emp("E-HAND", "인수인계자")
    session.add_all([leaver, reviewer, handover])
    await session.flush()
    a = await create_asset(AssetBody(asset_tag="OFF-1", name="회수대상"), user=_ADMIN, session=session)
    await assign_asset(a["id"], AssignBody(employee_id=leaver.id), user=_ADMIN, session=session)
    # 기록 없음
    assert (await get_offboarding(leaver.id, user=_ADMIN, session=session))["record"] is None
    o = await create_offboarding(OffboardingBody(employee_id=leaver.id, reviewer_id=reviewer.id,
                                                 handover_to_id=handover.id, note="장비 정상 회수"),
                                 user=_ADMIN, session=session)
    assert o["reviewer_name"] == "검토자" and o["handover_to_name"] == "인수인계자"
    # 재생성 멱등
    o2 = await create_offboarding(OffboardingBody(employee_id=leaver.id), user=_ADMIN, session=session)
    assert o2["id"] == o["id"]
    # 미회수 자산 남음 → 완료 차단
    with pytest.raises(ValidationError):
        await complete_offboarding(o["id"], user=_ADMIN, session=session)
    # 회수 후 완료 가능
    await return_asset(a["id"], ReturnBody(), user=_ADMIN, session=session)
    upd = await patch_offboarding(o["id"], OffboardingPatch(note="반납 완료"), user=_ADMIN, session=session)
    assert upd["note"] == "반납 완료"
    done = await complete_offboarding(o["id"], user=_ADMIN, session=session)
    assert done["status"] == "COMPLETED" and done["completed_on"] is not None


async def test_it_cost_gl_posting(session):
    """R4·F-6 IT 비용 GL 전기 — DR 5525/CR 2110·건별 멱등·재전기 방지·미전기 현황."""
    from sqlalchemy import select

    from erp.finance.models import Voucher, VoucherLine
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, cancel_service, create_service
    cs = CostService()
    s = await create_service(ServiceBody(name="AWS", vendor="AWS", cost_center="영상의학",
                                         billing_model="USAGE_METERED"), user=_ADMIN, session=session)
    s2 = await create_service(ServiceBody(name="해지SaaS", billing_model="SEAT_RECURRING"),
                              user=_ADMIN, session=session)
    await cs.record_cost(session, actor=1, service_id=s["id"], period="2026-07", amount=500000)
    await cs.record_cost(session, actor=1, service_id=s2["id"], period="2026-07", amount=100000)
    # 재검토 — 서비스 해지되어도 발생한 비용은 전기(상태 무관·유령/미전기화 방지)
    await cancel_service(s2["id"], user=_ADMIN, session=session)
    st0 = await cs.posting_status(session, "2026-07")
    assert st0["unposted"] == 2 and st0["unposted_krw"] == 600000   # 상태 무관·전기 사실 기준
    # 전기 실행 — 해지분 포함 2건
    out = await cs.post_period(session, actor=9, period="2026-07")
    assert out["posted"] == 2 and out["total_krw"] == 600000 and out["account"] == "5525"
    # 전표 검증 — 전체 DR 5525 / CR 2110 합계 균형
    vlines = (await session.scalars(select(VoucherLine).join(
        Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.source_module == "itam.cost"))).all()
    dr = sum(int(ln.amount) for ln in vlines if ln.side == "DEBIT" and ln.account_code == "5525")
    cr = sum(int(ln.amount) for ln in vlines if ln.side == "CREDIT" and ln.account_code == "2110")
    assert dr == 600000 and cr == 600000
    # 멱등 — 재실행 시 이미 전기분 제외
    out2 = await cs.post_period(session, actor=9, period="2026-07")
    assert out2["posted"] == 0 and out2["skipped"] >= 2
    st1 = await cs.posting_status(session, "2026-07")
    assert st1["posted"] == 2 and st1["unposted"] == 0


async def test_procurement_po_to_asset(session):
    """R4 조달 PO 연계 — 입고된 EQUIPMENT → IT 자산 등록(프리필·잔여 가드·후보 제외)."""
    from datetime import datetime as _dt, timezone as _tz

    from erp.itam.router import ProcurementRegisterBody, procurement_candidates, register_from_po
    from erp.scm.models import Item, PoLine, PurchaseOrder, Supplier
    sup = Supplier(name="델테크놀로지")
    item = Item(code="EQ-NB", name="비즈니스 노트북", category="EQUIPMENT", unit="EA")
    session.add_all([sup, item])
    await session.flush()
    po = PurchaseOrder(supplier_id=sup.id, status="RECEIVED", requested_by=1,
                       department_id=8, received_at=_dt(2026, 7, 1, tzinfo=_tz.utc))
    session.add(po)
    await session.flush()
    pl = PoLine(po_id=po.id, item_id=item.id, qty=2, unit_price=1500000, received_qty=2)
    session.add(pl)
    await session.flush()
    # 후보 — 잔여 2
    cand = await procurement_candidates(user=_ADMIN, session=session)
    c = next(x for x in cand["items"] if x["po_line_id"] == pl.id)
    assert c["remaining"] == 2 and c["item_name"] == "비즈니스 노트북" and c["supplier"] == "델테크놀로지"
    # 1대 등록 — 품목명·공급사·도입일·부서 프리필
    a = await register_from_po(pl.id, ProcurementRegisterBody(category="LAPTOP", serial_no="SN-P1"),
                               user=_ADMIN, session=session)
    assert a["name"] == "비즈니스 노트북" and a["manufacturer"] == "델테크놀로지"
    assert a["owner_department_id"] == 8 and str(a["acquired_on"]) == "2026-07-01"
    assert a["procurement_po_line_id"] == pl.id and a["category"] == "LAPTOP"
    # 잔여 1로 감소
    cand2 = await procurement_candidates(user=_ADMIN, session=session)
    assert next(x for x in cand2["items"] if x["po_line_id"] == pl.id)["remaining"] == 1
    # 2대째 등록 후 후보 제외
    await register_from_po(pl.id, ProcurementRegisterBody(), user=_ADMIN, session=session)
    cand3 = await procurement_candidates(user=_ADMIN, session=session)
    assert not any(x["po_line_id"] == pl.id for x in cand3["items"])
    # 초과 등록 차단
    with pytest.raises(ValidationError):
        await register_from_po(pl.id, ProcurementRegisterBody(), user=_ADMIN, session=session)


async def test_shadow_it_reconcile(session):
    """R4 Shadow-IT 대사 — 알려진 IT 벤더=대사, IT성 미등록 벤더=shadow 후보, 비IT=제외."""
    from datetime import date as _date

    from erp.finance.models import Payable, TaxInvoice
    from erp.itam.router import ServiceBody, create_service, shadow_it
    # 알려진 IT 벤더 등록
    await create_service(ServiceBody(name="클라우드", vendor="AWS", billing_model="USAGE_METERED"),
                         user=_ADMIN, session=session)
    today = _date.today()
    session.add_all([
        Payable(party="AWS", description="클라우드 사용료", amount=500000,
                incurred_on=today, due_on=today),                       # 관리(알려진 벤더)
        Payable(party="Adobe Korea", description="Creative Cloud 구독", amount=200000,
                incurred_on=today, due_on=today),                       # shadow(IT키워드·미등록)
        Payable(party="한국제약", description="의약품 구매", amount=900000,
                incurred_on=today, due_on=today),                       # 비IT(제외)
        TaxInvoice(approval_no="TI-SH-1", direction="IN", issue_date=today,
                   supplier_bizno="1112233445", supplier_name="Microsoft Korea",
                   supply_amount=300000, vat_amount=30000, total_amount=330000),  # shadow(IT키워드)
    ])
    await session.flush()
    out = await shadow_it(months=12, user=_ADMIN, session=session)
    managed_parties = {r["party"] for r in out["managed"]}
    shadow_parties = {r["party"] for r in out["shadow"]}
    assert "AWS" in managed_parties
    assert "Adobe Korea" in shadow_parties and "Microsoft Korea" in shadow_parties
    assert "한국제약" not in shadow_parties and "한국제약" not in managed_parties   # 비IT 제외
    assert out["shadow_total"] == 200000 + 330000 and out["managed_total"] == 500000


async def test_posted_cost_edit_blocked(session):
    """재검토 HIGH — GL 전기 후 금액 수정 차단(원장↔GL 불일치 방지)."""
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, create_service
    cs = CostService()
    s = await create_service(ServiceBody(name="편집SaaS", billing_model="SEAT_RECURRING"),
                             user=_ADMIN, session=session)
    await cs.record_cost(session, actor=1, service_id=s["id"], period="2026-07", amount=100000)
    await cs.post_period(session, actor=1, period="2026-07")            # 전기
    # 동일 금액 재입력은 허용(멱등), 금액 변경은 차단
    await cs.record_cost(session, actor=1, service_id=s["id"], period="2026-07", amount=100000)
    with pytest.raises(ValidationError, match="전기된"):
        await cs.record_cost(session, actor=1, service_id=s["id"], period="2026-07", amount=300000)


async def test_shadow_it_dedup_and_void(session):
    """재검토 MED — Shadow-IT: AP 연결 세금계산서(payable_id) 이중집계 제거·VOID 제외."""
    from datetime import date as _date

    from erp.finance.models import Payable, TaxInvoice
    from erp.itam.router import shadow_it
    today = _date.today()
    ap = Payable(party="Adobe", description="구독", amount=1200000, incurred_on=today, due_on=today)
    session.add(ap)
    await session.flush()
    session.add_all([
        # 연결 세금계산서 → AP로 이미 집계되므로 제외(이중집계 방지)
        TaxInvoice(approval_no="TI-D1", direction="IN", issue_date=today, supplier_bizno="1",
                   supplier_name="Adobe", supply_amount=1000000, vat_amount=100000,
                   total_amount=1100000, payable_id=ap.id),
        # VOID → 제외
        TaxInvoice(approval_no="TI-D2", direction="IN", issue_date=today, supplier_bizno="2",
                   supplier_name="클라우드호스팅", supply_amount=500000, vat_amount=50000,
                   total_amount=550000, status="VOID"),
    ])
    await session.flush()
    out = await shadow_it(months=12, user=_ADMIN, session=session)
    parties = [r["party"] for r in out["shadow"]]
    assert parties.count("Adobe") == 1                 # AP 1건만(연결 세금계산서 제외)
    assert "클라우드호스팅" not in parties              # VOID 제외
    assert out["shadow_total"] == 1200000              # AP만


def test_norm_vendor_token_based():
    """재검토 LOW — 벤더 정규화는 토큰 단위(부분문자열 과다제거·순서의존 오류 없음)."""
    from erp.itam.cost_service import CostService
    n = CostService._norm_vendor
    # 법인격 접미어는 토큰일 때만 제거 — 'Principal'의 'inc'/'corporation'의 'corp'는 손상 안 됨
    assert n("Principal Financial") == "principalfinancial"
    assert n("Adobe Corporation") == "adobe"           # corporation 토큰 제거
    assert n("(주)한국오라클") == "한국오라클"
    assert n("Amazon Web Services Inc.") == "amazonwebservices"
    # 서로 다른 벤더가 같은 값으로 붕괴하지 않음
    assert n("Oracle") != n("Adobe")


async def test_disposed_maintenance_attachment_delete_blocked(session):
    """재검토 LOW — 폐기 자산의 유지보수 증빙 삭제 차단(오분류 경로 보호)."""
    from erp.itam.attachment_guards import _guard_disposed_maintenance
    from erp.itam.router import DisposeBody, MaintenanceBody, add_maintenance, create_asset, dispose_asset
    a = await create_asset(AssetBody(asset_tag="MNT-D", name="폐기예정", handles_phi=True),
                           user=_ADMIN, session=session)
    m = await add_maintenance(a["id"], MaintenanceBody(kind="CALIBRATION", on_date=date(2026, 7, 1)),
                              user=_ADMIN, session=session)
    await _guard_disposed_maintenance(session, m["id"])   # 폐기 전 → 통과
    await dispose_asset(a["id"], DisposeBody(data_erased=True), user=_ADMIN, session=session)
    with pytest.raises(ValidationError):                  # 폐기 후 → 유지보수 증빙 삭제 차단
        await _guard_disposed_maintenance(session, m["id"])
