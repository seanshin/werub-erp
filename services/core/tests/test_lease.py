"""리스 회계 (K-IFRS 16) — 개시 인식·월별 이자·원금·상각·멱등."""
from datetime import date

from sqlalchemy import select

from erp.finance.lease_service import LeaseService
from erp.finance.models import Account, Voucher

svc = LeaseService()


async def _seed(session):
    session.add_all([Account(code=c, name=c, kind=k) for c, k in [
        ("1720", "ASSET"), ("1825", "ASSET"), ("2210", "LIABILITY"),
        ("5630", "EXPENSE"), ("5640", "EXPENSE"), ("1110", "ASSET")]])
    await session.flush()


async def test_create_recognizes_rou_and_liability(session):
    await _seed(session)
    # 무이자 24개월 × 100만 = 부채 2,400만
    lease = await svc.create(session, lease_no="LS-1", asset_name="MRI",
                             start_on=date(2026, 1, 1), term_months=24,
                             monthly_payment=1_000_000, annual_rate=0)
    assert float(lease.liability_initial) == 24_000_000
    assert float(lease.rou_initial) == 24_000_000
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == f"lease-start:{lease.id}"))
    assert v is not None


async def test_accrue_reduces_liability_and_amortizes(session):
    await _seed(session)
    lease = await svc.create(session, lease_no="LS-2", asset_name="X",
                             start_on=date(2026, 1, 1), term_months=24,
                             monthly_payment=1_000_000, annual_rate=0)
    await session.flush()
    out = await svc.accrue_monthly(session, lease_id=lease.id, period="2026-01")
    # 무이자: 이자 0·원금 100만·상각 100만(2400만/24)
    assert out["interest"] == 0 and out["principal"] == 1_000_000 and out["amortization"] == 1_000_000
    assert out["liability_after"] == 23_000_000
    # 멱등
    again = await svc.accrue_monthly(session, lease_id=lease.id, period="2026-01")
    assert again.get("already")


async def test_interest_split(session):
    await _seed(session)
    lease = await svc.create(session, lease_no="LS-3", asset_name="Y",
                             start_on=date(2026, 1, 1), term_months=12,
                             monthly_payment=1_000_000, annual_rate=0.12)  # 월 1%
    await session.flush()
    out = await svc.accrue_monthly(session, lease_id=lease.id, period="2026-01")
    assert out["interest"] > 0 and out["principal"] == 1_000_000 - out["interest"]


async def test_full_term_zeros_liability_and_rou(session):
    await _seed(session)
    lease = await svc.create(session, lease_no="LS-F", asset_name="Z",
                             start_on=date(2026, 1, 1), term_months=12,
                             monthly_payment=1_000_000, annual_rate=0.12)
    await session.flush()
    for i in range(12):
        ym = f"2026-{i + 1:02d}"
        await svc.accrue_monthly(session, lease_id=lease.id, period=ym)
        await session.flush()
    # 최종기 true-up: 리스부채·사용권자산 모두 0
    assert round(float(lease.current_liability)) == 0
    assert round(float(lease.rou_initial) - float(lease.accumulated_amort)) == 0
    assert lease.status == "ENDED"
