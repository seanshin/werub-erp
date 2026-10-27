"""고정자산 부서이동·실사(0218) — 이동 이력·department_id 갱신·실사 개시/확인/마감·불일치 집계."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.asset_ops_service import AssetOpsService
from erp.finance.models import AssetInventoryLine, FixedAsset

svc = AssetOpsService()
ON = date(2026, 7, 21)


async def _asset(session, no="A1", dept=3, status="IN_USE"):
    a = FixedAsset(asset_no=no, name="MRI", category="MEDICAL", acquired_on=date(2025, 1, 1),
                   acquisition_cost=1_000_000, useful_life_years=5, department_id=dept,
                   status=status)
    session.add(a)
    await session.flush()
    return a


async def test_transfer_records_history_and_updates_dept(session):
    a = await _asset(session, dept=3)
    tr = await svc.transfer(session, asset_id=a.id, to_dept=7, transferred_on=ON,
                            reason="부서 재배치", actor=2)
    assert tr.from_dept == 3 and tr.to_dept == 7
    assert a.department_id == 7                       # department_id 갱신
    hist = await svc.transfer_history(session, asset_id=a.id)
    assert len(hist) == 1 and hist[0]["from_dept"] == 3
    # 2차 이동 이력 누적
    await svc.transfer(session, asset_id=a.id, to_dept=9, transferred_on=ON, actor=2)
    assert len(await svc.transfer_history(session, asset_id=a.id)) == 2


async def test_transfer_same_dept_and_disposed_blocked(session):
    a = await _asset(session, dept=3)
    with pytest.raises(ValidationError, match="동일"):
        await svc.transfer(session, asset_id=a.id, to_dept=3, transferred_on=ON)
    d = await _asset(session, no="A2", status="DISPOSED")
    with pytest.raises(ValidationError, match="사용 중"):
        await svc.transfer(session, asset_id=d.id, to_dept=5, transferred_on=ON)


async def test_inventory_lifecycle(session):
    """개시(IN_USE 스냅샷)→라인 확인(정위치/이전/소재불명)→마감 집계."""
    a1 = await _asset(session, no="A1", dept=3)
    a2 = await _asset(session, no="A2", dept=3)
    a3 = await _asset(session, no="A3", dept=5)
    await _asset(session, no="A4", status="DISPOSED")     # 제외
    inv = await svc.open_inventory(session, name="2026 실사", conducted_on=ON, actor=1)
    lines = (await session.scalars(select(AssetInventoryLine).where(
        AssetInventoryLine.inventory_id == inv.id))).all()
    assert len(lines) == 3                                 # IN_USE 3건만
    by_asset = {ln.asset_id: ln for ln in lines}
    await svc.check_line(session, line_id=by_asset[a1.id].id, found_status="FOUND", actor=2)
    await svc.check_line(session, line_id=by_asset[a2.id].id, found_status="RELOCATED",
                         actual_dept=8, actor=2)
    await svc.check_line(session, line_id=by_asset[a3.id].id, found_status="NOT_FOUND", actor=2)
    out = await svc.close_inventory(session, inventory_id=inv.id, actor=1)
    assert out["status"] == "CLOSED"
    assert out["found"] == 1 and out["relocated"] == 1 and out["not_found"] == 1
    assert out["not_checked"] == 0 and out["total"] == 3


async def test_relocated_requires_actual_dept(session):
    await _asset(session)
    inv = await svc.open_inventory(session, name="x", conducted_on=ON)
    line = (await session.scalars(select(AssetInventoryLine).where(
        AssetInventoryLine.inventory_id == inv.id))).first()
    with pytest.raises(ValidationError, match="실제 부서"):
        await svc.check_line(session, line_id=line.id, found_status="RELOCATED")


async def test_closed_inventory_immutable(session):
    await _asset(session)
    inv = await svc.open_inventory(session, name="x", conducted_on=ON)
    line = (await session.scalars(select(AssetInventoryLine).where(
        AssetInventoryLine.inventory_id == inv.id))).first()
    await svc.close_inventory(session, inventory_id=inv.id)
    with pytest.raises(ValidationError, match="마감"):
        await svc.check_line(session, line_id=line.id, found_status="FOUND")
    with pytest.raises(ValidationError, match="이미 마감"):
        await svc.close_inventory(session, inventory_id=inv.id)


async def test_duplicate_open_inventory_blocked(session):
    """진행 중(OPEN) 실사가 있으면 개시 차단·마감 후 재개시 허용(중복 OPEN 가드)."""
    await _asset(session)
    inv = await svc.open_inventory(session, name="1차", conducted_on=ON)
    with pytest.raises(ValidationError, match="진행 중인 실사"):
        await svc.open_inventory(session, name="2차", conducted_on=ON)
    await svc.close_inventory(session, inventory_id=inv.id)
    inv2 = await svc.open_inventory(session, name="2차", conducted_on=ON)   # 마감 후 허용
    assert inv2.status == "OPEN"


async def test_discrepancy_only_filter(session):
    a1 = await _asset(session, no="A1")
    a2 = await _asset(session, no="A2")
    inv = await svc.open_inventory(session, name="x", conducted_on=ON)
    lines = {ln.asset_id: ln for ln in (await session.scalars(select(AssetInventoryLine)
             .where(AssetInventoryLine.inventory_id == inv.id))).all()}
    await svc.check_line(session, line_id=lines[a1.id].id, found_status="FOUND", actor=2)
    await svc.check_line(session, line_id=lines[a2.id].id, found_status="NOT_FOUND", actor=2)
    detail = await svc.inventory_detail(session, inventory_id=inv.id, discrepancy_only=True)
    statuses = {ln["found_status"] for ln in detail["lines"]}
    assert statuses == {"NOT_FOUND"}                       # FOUND 제외
