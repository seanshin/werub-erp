"""P2-2b — 공정가치 서열(1113)·중간보고(1034)·전기비교(1001)."""
from datetime import date

from erp.finance.disclosure_service import DisclosureService
from erp.finance.models import Account, FixedAsset
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.statements import StatementService

ss = StatementService()
ds = DisclosureService()
vs = VoucherService()


async def _accts(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="4110", name="수익", kind="REVENUE"),
        Account(code="5110", name="비용", kind="EXPENSE"),
        Account(code="1710", name="의료장비", kind="ASSET"),
    ])
    await session.flush()


async def test_interim_report(session):
    await _accts(session)
    # 2026 2월(1분기) 수익 100만, 5월(2분기) 수익 200만
    for mon, amt in [(2, 1000000), (5, 2000000)]:
        await vs.create(session, source_module="manual", source_ref=f"r{mon}",
                        voucher_on=date(2026, mon, 10),
                        lines=[VoucherLineDTO("1110", "DEBIT", amt),
                               VoucherLineDTO("4110", "CREDIT", amt)])
    q1 = await ss.interim_report(session, year=2026, quarter=1)
    assert q1["quarterly"]["revenue"] == 1000000
    q2 = await ss.interim_report(session, year=2026, quarter=2)
    assert q2["quarterly"]["revenue"] == 2000000
    assert q2["cumulative"]["revenue"] == 3000000      # YTD 1~6월


async def test_comparative_balance_sheet(session):
    await _accts(session)
    await vs.create(session, source_module="manual", source_ref="cur",
                    voucher_on=date(2026, 3, 1),
                    lines=[VoucherLineDTO("1110", "DEBIT", 5000000),
                           VoucherLineDTO("4110", "CREDIT", 5000000)])
    await vs.create(session, source_module="manual", source_ref="pri",
                    voucher_on=date(2025, 3, 1),
                    lines=[VoucherLineDTO("1110", "DEBIT", 2000000),
                           VoucherLineDTO("4110", "CREDIT", 2000000)])
    out = await ss.comparative_balance_sheet(session, as_of=date(2026, 12, 31))
    assert out["current"]["total_assets"] == 7000000   # 누적 5백+2백만
    assert out["prior"]["total_assets"] == 2000000
    assert out["delta"]["total_assets"] == 5000000


async def test_fair_value_hierarchy(session):
    await _accts(session)
    session.add_all([
        FixedAsset(asset_no="L2", name="건물", category="MEDICAL", acquired_on=date(2024, 1, 1),
                   acquisition_cost=100000000, useful_life_years=20, fair_value_level="2"),
        FixedAsset(asset_no="L3", name="장비", category="MEDICAL", acquired_on=date(2024, 1, 1),
                   acquisition_cost=30000000, useful_life_years=5, fair_value_level="3"),
    ])
    await session.flush()
    fv = await ds.fair_value_hierarchy(session)
    levels = {x["level"]: x["book_value"] for x in fv["levels"]}
    assert levels["2"] == 100000000 and levels["3"] == 30000000
    assert fv["total"] == 130000000
