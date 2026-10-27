"""IT자산 리스 취득(0227) — IFRS16 리스계약 연결·상호배타·전표·요약·HTTP."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.models import LeaseContract, Voucher, VoucherLine
from erp.itam.service import ITAMService

svc = ITAMService()
ON = date(2026, 7, 1)


async def test_attach_lease_creates_ifrs16_and_links(session):
    """리스 개시 — 사용권자산(ROU)/리스부채 인식 전표 + 자산 acquisition_type=LEASED·백링크."""
    a = await svc.create_asset(session, actor=1, asset_tag="L-1", name="리스랩탑")
    out = await svc.attach_lease(session, actor=1, asset_id=a.id, start_on=ON,
                                 term_months=36, monthly_payment=100_000, annual_rate=0.06)
    assert out["lease_id"] and out["lease_no"] == "ITL-L-1" and out["rou_initial"] > 0
    assert a.acquisition_type == "LEASED" and a.lease_id == out["lease_id"]
    assert a.acquired_on == ON                                   # 미설정 시 개시일 반영
    lease = await session.get(LeaseContract, out["lease_id"])
    assert lease.asset_kind == "IT" and lease.itam_asset_id == a.id
    # 개시 전표 DR 사용권자산 / CR 리스부채
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == f"lease-start:{lease.id}"))
    assert v is not None
    sides = {ln.side for ln in (await session.scalars(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).all()}
    assert sides == {"DEBIT", "CREDIT"}
    summ = await svc.lease_summary(session, a.id)
    assert summ["lease_no"] == "ITL-L-1" and summ["current_liability"] > 0


async def test_lease_mutually_exclusive_with_purchase(session):
    """구매(finance_asset_id) 취득 자산엔 리스 불가·리스 중복 연결 불가."""
    a = await svc.create_asset(session, actor=1, asset_tag="L-2", name="구매랩탑")
    a.finance_asset_id = 999
    await session.flush()
    with pytest.raises(ValidationError, match="구매"):
        await svc.attach_lease(session, actor=1, asset_id=a.id, start_on=ON,
                               term_months=12, monthly_payment=50_000)
    b = await svc.create_asset(session, actor=1, asset_tag="L-3", name="리스랩탑2")
    await svc.attach_lease(session, actor=1, asset_id=b.id, start_on=ON,
                           term_months=24, monthly_payment=80_000)
    with pytest.raises(ValidationError, match="이미 리스"):
        await svc.attach_lease(session, actor=1, asset_id=b.id, start_on=ON,
                               term_months=24, monthly_payment=80_000)


async def test_exempt_lease_no_voucher(session):
    """단기·소액 면제 리스 — 인식 전표 없음(월 리스료 직접 비용)·요약 exempt=True."""
    a = await svc.create_asset(session, actor=1, asset_tag="L-4", name="단기리스")
    out = await svc.attach_lease(session, actor=1, asset_id=a.id, start_on=ON,
                                 term_months=6, monthly_payment=30_000, exempt=True)
    assert out["exempt"] is True and out["liability_initial"] == 0
    lease = await session.get(LeaseContract, out["lease_id"])
    assert await session.scalar(select(Voucher).where(
        Voucher.source_ref == f"lease-start:{lease.id}")) is None


async def test_http_attach_lease_and_detail(session, client):
    """HTTP — 리스 연결(201)·상세에 lease 요약·acquisition_type 반영. STAFF는 write 불가."""
    from tests._http import auth_as, bearer, make_user
    headers = await auth_as(session, user_id=1, role="IT_MANAGER")
    a = await svc.create_asset(session, actor=1, asset_tag="L-H1", name="HTTP리스")
    await session.flush()
    r = await client.post(f"/api/v1/itam/assets/{a.id}/lease", headers=headers,
                          json={"start_on": "2026-07-01", "term_months": 36,
                                "monthly_payment": 120000, "annual_rate": 0.05})
    assert r.status_code == 201 and r.json()["lease_no"] == "ITL-L-H1"
    r = await client.get(f"/api/v1/itam/assets/{a.id}", headers=headers)
    body = r.json()
    assert body["acquisition_type"] == "LEASED" and body["lease"]["term_months"] == 36
    await make_user(session, user_id=5, role="STAFF")
    r = await client.post(f"/api/v1/itam/assets/{a.id}/lease",
                          headers=bearer(user_id=5, role="STAFF"),
                          json={"start_on": "2026-07-01", "term_months": 12,
                                "monthly_payment": 10000})
    assert r.status_code == 403
