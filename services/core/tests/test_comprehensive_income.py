"""포괄손익계산서 (K-IFRS 1001) — 당기순이익 + OCI(재평가·보험수리) = 총포괄손익."""
from datetime import date

from erp.finance.models import Account
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.statements import StatementService

svc = StatementService()
vs = VoucherService()


async def _seed(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1710", name="의료장비", kind="ASSET"),
        Account(code="4110", name="수익", kind="REVENUE"),
        Account(code="5110", name="비용", kind="EXPENSE"),
        Account(code="3400", name="재평가잉여금", kind="CAPITAL"),
        Account(code="3410", name="보험수리적손익", kind="CAPITAL"),
        Account(code="2120", name="퇴직급여충당부채", kind="LIABILITY"),
    ])
    await session.flush()


async def test_comprehensive_income(session):
    await _seed(session)
    # 당기순이익: 수익 1,000,000 − 비용 400,000 = 600,000
    await vs.create(session, source_module="manual", source_ref="rev",
                    voucher_on=date(2026, 3, 1),
                    lines=[VoucherLineDTO("1110", "DEBIT", 1000000),
                           VoucherLineDTO("4110", "CREDIT", 1000000)])
    await vs.create(session, source_module="manual", source_ref="exp",
                    voucher_on=date(2026, 3, 1),
                    lines=[VoucherLineDTO("5110", "DEBIT", 400000),
                           VoucherLineDTO("1110", "CREDIT", 400000)])
    # OCI: 재평가잉여금 +300,000(이익), 보험수리적손실 100,000(차변, OCI −)
    await vs.create(session, source_module="manual", source_ref="reval",
                    voucher_on=date(2026, 3, 10),
                    lines=[VoucherLineDTO("1710", "DEBIT", 300000),
                           VoucherLineDTO("3400", "CREDIT", 300000)])
    await vs.create(session, source_module="closing-ifrs", source_ref="dbo-remeasure:2026-03",
                    voucher_on=date(2026, 3, 31),
                    lines=[VoucherLineDTO("3410", "DEBIT", 100000),
                           VoucherLineDTO("2120", "CREDIT", 100000)])
    ci = await svc.comprehensive_income(session, since=date(2026, 3, 1), until=date(2026, 3, 31))
    assert ci["net_income"] == 600000
    # OCI = +300,000(재평가) − 100,000(보험수리손실) = +200,000
    assert ci["oci_total"] == 200000
    assert ci["total_comprehensive_income"] == 800000
    assert {x["account_code"] for x in ci["oci_items"]} == {"3400", "3410"}
