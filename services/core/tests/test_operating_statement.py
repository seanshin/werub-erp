"""의료기관 회계기준 운영성과표 — 의료/의료외/법인세 분류·의료이익·당기순이익."""
from datetime import date

from erp.finance.models import Account
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.statements import StatementService

vs = VoucherService()
st = StatementService()


async def _acct(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="4110", name="외래수익", kind="REVENUE"),       # 의료수익
        Account(code="5110", name="의약품비", kind="EXPENSE"),       # 의료비용
        Account(code="4630", name="외환차익", kind="REVENUE"),       # 의료외수익
        Account(code="5660", name="외환차손", kind="EXPENSE"),       # 의료외비용
        Account(code="5810", name="법인세비용", kind="EXPENSE"),     # 법인세
    ])
    await session.flush()


async def _post(session, ref, lines, on=date(2026, 6, 15)):
    await vs.create(session, source_module="test", source_ref=ref, voucher_on=on,
                    lines=[VoucherLineDTO(*line) for line in lines], memo=ref)


async def test_operating_statement_classification(session):
    await _acct(session)
    await _post(session, "rev", [("1110", "DEBIT", 100_000), ("4110", "CREDIT", 100_000)])
    await _post(session, "exp", [("5110", "DEBIT", 30_000), ("1110", "CREDIT", 30_000)])
    await _post(session, "nonrev", [("1110", "DEBIT", 5_000), ("4630", "CREDIT", 5_000)])
    await _post(session, "nonexp", [("5660", "DEBIT", 2_000), ("1110", "CREDIT", 2_000)])
    await _post(session, "tax", [("5810", "DEBIT", 10_000), ("1110", "CREDIT", 10_000)])

    r = await st.operating_statement(session, since=date(2026, 6, 1), until=date(2026, 6, 30))
    assert r["medical_revenue"] == 100_000 and r["medical_expense"] == 30_000
    assert r["medical_income"] == 70_000                  # 의료이익
    assert r["non_medical_revenue"] == 5_000 and r["non_medical_expense"] == 2_000
    assert r["pretax_income"] == 73_000                   # 70,000 + 5,000 − 2,000
    assert r["income_tax"] == 10_000 and r["net_income"] == 63_000
