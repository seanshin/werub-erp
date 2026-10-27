"""ERP 마스터코드 회신(HIS 연동) — 품목·자산·직원 코드 조회 EP."""
from datetime import date

import pytest

from erp.app.admin_router import list_master_codes
from erp.common.router import CurrentUser
from erp.errors import ValidationError

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=1, license_flag=True)


async def test_item_master_codes_erp_own(session):
    """품목 — ERP 자체코드(mapping=ERP_OWN_CODE)."""
    from erp.scm.models import Item
    session.add_all([
        Item(code="MED-001", name="의약품1", category="DRUG", is_narcotic=True),
        Item(code="SUP-001", name="거즈", category="SUPPLY")])
    await session.flush()
    out = await list_master_codes(kind="item", user=ADMIN, session=session)
    assert out["mapping"] == "ERP_OWN_CODE" and out["count"] == 2
    codes = {i["erp_item_code"]: i for i in out["items"]}
    assert codes["MED-001"]["is_narcotic"] is True and codes["SUP-001"]["category"] == "SUPPLY"


async def test_asset_master_codes_identity_and_active_filter(session):
    """자산 — HIS 식별자 1:1(mapping=IDENTITY)·active_only는 처분자산 제외."""
    from erp.finance.models import FixedAsset
    session.add_all([
        FixedAsset(asset_no="EQ-CT-001", name="CT", category="MEDICAL",
                   acquired_on=date(2026, 1, 1), acquisition_cost=1_000_000,
                   useful_life_years=8, status="IN_USE"),
        FixedAsset(asset_no="EQ-OLD-001", name="폐장비", category="MEDICAL",
                   acquired_on=date(2020, 1, 1), acquisition_cost=1_000_000,
                   useful_life_years=8, status="DISPOSED")])
    await session.flush()
    out = await list_master_codes(kind="asset", active_only=True, user=ADMIN, session=session)
    assert out["mapping"] == "IDENTITY"
    codes = {i["erp_asset_code"] for i in out["items"]}
    assert "EQ-CT-001" in codes and "EQ-OLD-001" not in codes   # 처분자산 제외
    # active_only=False면 전부
    allout = await list_master_codes(kind="asset", active_only=False, user=ADMIN, session=session)
    assert {i["erp_asset_code"] for i in allout["items"]} >= {"EQ-CT-001", "EQ-OLD-001"}


async def test_employee_master_codes_identity_and_active(session):
    """직원 — 사번 1:1(mapping=IDENTITY)·Clinic id·재직자만(active_only)."""
    from erp.hr.models import Employee
    session.add_all([
        Employee(employee_no="E0001", name="재직", source="HIS", hired_on=date(2026, 1, 1),
                 external_user_id="clinic-1"),
        Employee(employee_no="E0002", name="퇴직", source="HIS", hired_on=date(2020, 1, 1),
                 resigned_on=date(2026, 6, 1))])
    await session.flush()
    out = await list_master_codes(kind="employee", active_only=True, user=ADMIN, session=session)
    assert out["mapping"] == "IDENTITY"
    codes = {i["erp_employee_code"]: i for i in out["items"]}
    assert "E0001" in codes and "E0002" not in codes            # 퇴직자 제외
    assert codes["E0001"]["external_user_id"] == "clinic-1" and codes["E0001"]["active"] is True


async def test_master_codes_rejects_unknown_kind(session):
    with pytest.raises(ValidationError, match="item.asset.employee"):
        await list_master_codes(kind="wrong", user=ADMIN, session=session)


async def test_item_master_codes_truncation_and_keyset_paging(session):
    """상한 도달 시 truncated 신호 + next_after 커서로 전량 페이지네이션(침묵 절단 방지)."""
    from erp.scm.models import Item
    session.add_all([Item(code=f"ITM-{i:03d}", name=f"품목{i}", category="SUPPLY")
                     for i in range(5)])
    await session.flush()
    # limit=2 → 상한 도달 → truncated + next_after
    p1 = await list_master_codes(kind="item", limit=2, user=ADMIN, session=session)
    assert p1["count"] == 2 and p1["truncated"] is True
    assert p1["next_after"] == "ITM-001" and [i["erp_item_code"] for i in p1["items"]] == \
        ["ITM-000", "ITM-001"]
    # after 커서로 다음 페이지 — 겹침 없이 이어짐
    p2 = await list_master_codes(kind="item", limit=2, after=p1["next_after"],
                                 user=ADMIN, session=session)
    assert [i["erp_item_code"] for i in p2["items"]] == ["ITM-002", "ITM-003"]
    # 마지막 페이지 — 상한 미도달 → truncated False·next_after None
    p3 = await list_master_codes(kind="item", limit=2, after=p2["next_after"],
                                 user=ADMIN, session=session)
    assert p3["items"][0]["erp_item_code"] == "ITM-004"
    assert p3["truncated"] is False and p3["next_after"] is None
