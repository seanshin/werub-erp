"""리스 정밀화 (K-IFRS 1116) — 단기·소액 면제·재측정·만기분석."""
from datetime import date

from sqlalchemy import select

from erp.finance.lease_service import LeaseService
from erp.finance.models import Account, Voucher, VoucherLine

svc = LeaseService()


async def _seed(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1720", name="사용권자산", kind="ASSET"),
        Account(code="1825", name="사용권상각누계", kind="ASSET"),
        Account(code="2210", name="리스부채", kind="LIABILITY"),
        Account(code="5630", name="리스이자", kind="EXPENSE"),
        Account(code="5640", name="사용권상각비", kind="EXPENSE"),
        Account(code="5645", name="단기·소액리스료", kind="EXPENSE"),
    ])
    await session.flush()


async def test_exempt_lease_direct_expense(session):
    await _seed(session)
    lease = await svc.create(session, lease_no="SL-1", asset_name="복합기", start_on=date(2026, 1, 1),
                             term_months=10, monthly_payment=200000, exempt=True)
    assert lease.exempt and float(lease.rou_initial) == 0
    # 개시 전표 없음
    assert await session.scalar(select(Voucher).where(
        Voucher.source_ref == f"lease-start:{lease.id}")) is None
    out = await svc.accrue_monthly(session, lease_id=lease.id, period="2026-01")
    assert out["exempt"] and out["lease_expense"] == 200000
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == f"lease:{lease.id}:2026-01"))
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert ("5645", "DEBIT") in sides and ("1110", "CREDIT") in sides


async def test_remeasure_adjusts_liability_and_rou(session):
    await _seed(session)
    lease = await svc.create(session, lease_no="L-1", asset_name="장비", start_on=date(2026, 1, 1),
                             term_months=12, monthly_payment=1000000, annual_rate=0)
    base = float(lease.current_liability)      # rate 0 → 12,000,000
    assert base == 12000000
    await svc.accrue_monthly(session, lease_id=lease.id, period="2026-01")  # 1기 경과
    # 지급액 1,200,000로 증액, 총기간 12 유지 → 잔여 11개월 × 120만 = 13,200,000
    out = await svc.remeasure(session, lease_id=lease.id, new_monthly_payment=1200000,
                              new_term_months=12, on_date=date(2026, 2, 1))
    assert out["liability_after"] == 13200000
    assert out["delta"] == 13200000 - 11000000   # 기존 잔여부채 1,100만 → +2,200,000


async def test_remeasure_amortizes_over_remaining_term(session):
    """심층 재검토 — 재측정 후 ROU 상각은 잔여기간 기준(전체 term 아님·최종기 큰 plug 제거·IFRS16)."""
    await _seed(session)
    lease = await svc.create(session, lease_no="LR", asset_name="장비", start_on=date(2026, 1, 1),
                             term_months=12, monthly_payment=1000000, annual_rate=0)
    await svc.accrue_monthly(session, lease_id=lease.id, period="2026-01")   # 1기 상각 1,000,000
    await svc.remeasure(session, lease_id=lease.id, new_monthly_payment=1200000,
                        new_term_months=12, on_date=date(2026, 2, 1))
    # ROU 장부가 13,200,000을 잔여 11개월로 정액 → 월 1,200,000
    # (구버전은 조정 rou_initial 14,200,000/12 = 1,183,333로 과소상각·최종기 plug)
    out = await svc.accrue_monthly(session, lease_id=lease.id, period="2026-02")
    assert out["amortization"] == 1200000


async def test_maturity_buckets(session):
    await _seed(session)
    await svc.create(session, lease_no="L-2", asset_name="장비2", start_on=date(2026, 1, 1),
                     term_months=24, monthly_payment=500000, annual_rate=0)
    m = await svc.maturity(session)
    b = {x["bucket"]: x["undiscounted"] for x in m["buckets"]}
    assert b["≤1년"] == 6000000          # 12개월 × 50만
    assert b["1-2년"] == 6000000         # 13~24개월 × 50만
    assert m["present_value_total"] == 12000000
