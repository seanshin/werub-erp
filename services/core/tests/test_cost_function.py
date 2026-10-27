"""기능별 비용 구분 (K-IFRS 1001) — 매출원가/판관비/기타·매출총이익·영업이익."""
from datetime import date

from erp.finance.account_service import AccountService
from erp.finance.models import Account
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.statements import StatementService

acc = AccountService()
ss = StatementService()
vs = VoucherService()


async def _seed(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="4110", name="의료수익", kind="REVENUE"),
        Account(code="5410", name="재료비", kind="EXPENSE", cost_function="COGS"),
        Account(code="5520", name="지급수수료", kind="EXPENSE", cost_function="SGA"),
        Account(code="5660", name="외환차손", kind="EXPENSE", cost_function="OTHER"),
    ])
    await session.flush()


async def test_income_by_function(session):
    await _seed(session)
    await vs.create(session, source_module="m", source_ref="rev", voucher_on=date(2026, 3, 1),
                    lines=[VoucherLineDTO("1110", "DEBIT", 10000000),
                           VoucherLineDTO("4110", "CREDIT", 10000000)])
    for code, amt, ref in [("5410", 4000000, "c"), ("5520", 2000000, "s"), ("5660", 500000, "o")]:
        await vs.create(session, source_module="m", source_ref=ref, voucher_on=date(2026, 3, 2),
                        lines=[VoucherLineDTO(code, "DEBIT", amt),
                               VoucherLineDTO("1110", "CREDIT", amt)])
    inc = await ss.income_statement(session, since=date(2026, 3, 1), until=date(2026, 3, 31))
    f = inc["by_function"]
    assert f["cogs"] == 4000000 and f["sga"] == 2000000 and f["other"] == 500000
    assert f["gross_profit"] == 6000000          # 1000만 − 400만
    assert f["operating_income"] == 4000000      # 600만 − 200만
    assert inc["net_income"] == 3500000          # 영업이익 − 기타


async def test_update_cost_function(session):
    await _seed(session)
    a = (await acc.list(session, kind="EXPENSE"))[0]
    out = await acc.update(session, account_id=a["id"], cost_function="SGA")
    assert out.cost_function == "SGA"
    cleared = await acc.update(session, account_id=a["id"], clear_cost_function=True)
    assert cleared.cost_function is None
