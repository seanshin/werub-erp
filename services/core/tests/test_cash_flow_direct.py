"""현금흐름표 직접법 (K-IFRS 1007) — 영업 수취·지급 분류·정합."""
from datetime import date

from erp.finance.models import Account
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.statements import StatementService

ss = StatementService()
vs = VoucherService()


async def _seed(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="4110", name="수익", kind="REVENUE"),
        Account(code="5310", name="급여", kind="EXPENSE"),
        Account(code="5410", name="재료비", kind="EXPENSE"),
        Account(code="1710", name="의료장비", kind="ASSET"),
    ])
    await session.flush()


async def test_direct_method(session):
    await _seed(session)
    # 고객수취: 현금 10,000,000 / 수익
    await vs.create(session, source_module="m", source_ref="rev", voucher_on=date(2026, 3, 1),
                    lines=[VoucherLineDTO("1110", "DEBIT", 10000000),
                           VoucherLineDTO("4110", "CREDIT", 10000000)])
    # 종업원지급: 급여 / 현금 3,000,000
    await vs.create(session, source_module="m", source_ref="pay", voucher_on=date(2026, 3, 5),
                    lines=[VoucherLineDTO("5310", "DEBIT", 3000000),
                           VoucherLineDTO("1110", "CREDIT", 3000000)])
    # 공급자지급: 재료비 / 현금 2,000,000
    await vs.create(session, source_module="m", source_ref="sup", voucher_on=date(2026, 3, 6),
                    lines=[VoucherLineDTO("5410", "DEBIT", 2000000),
                           VoucherLineDTO("1110", "CREDIT", 2000000)])
    # 투자: 장비 / 현금 5,000,000
    await vs.create(session, source_module="m", source_ref="inv", voucher_on=date(2026, 3, 7),
                    lines=[VoucherLineDTO("1710", "DEBIT", 5000000),
                           VoucherLineDTO("1110", "CREDIT", 5000000)])
    cf = await ss.cash_flow_direct(session, since=date(2026, 3, 1), until=date(2026, 3, 31))
    assert cf["operating"]["고객수취"] == 10000000
    assert cf["operating"]["종업원지급"] == -3000000
    assert cf["operating"]["공급자기타지급"] == -2000000
    assert cf["operating_net"] == 5000000
    assert cf["investing_net"] == -5000000
    assert cf["net_change"] == 0 and cf["reconciled"] is True
