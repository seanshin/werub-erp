"""자금수지 예측 — 현금잔액 + AR/AP due 기반 월별 포지션."""
from datetime import date

from erp.finance.models import Account, Payable, Receivable
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.settlement_service import SettlementService

svc = SettlementService()
vs = VoucherService()


async def _seed(session):
    session.add_all([Account(code="1110", name="현금", kind="ASSET"),
                     Account(code="4110", name="수익", kind="REVENUE")])
    await session.flush()
    # 기초 현금 5,000,000 (현금 차변)
    await vs.create(session, source_module="manual", source_ref="cash-open",
                    voucher_on=date(2026, 5, 1),
                    lines=[VoucherLineDTO("1110", "DEBIT", 5_000_000),
                           VoucherLineDTO("4110", "CREDIT", 5_000_000)])
    # AR 3,000,000 due 2026-07, AP 2,000,000 due 2026-06
    session.add(Receivable(party="공단", amount=3_000_000, collected_amount=0,
                           billed_on=date(2026, 5, 1), due_on=date(2026, 7, 15), status="OUTSTANDING"))
    session.add(Payable(party="공급사", amount=2_000_000, paid_amount=0,
                        incurred_on=date(2026, 5, 1), due_on=date(2026, 6, 20), status="OUTSTANDING"))
    await session.flush()


async def test_forecast_projects_balance(session):
    await _seed(session)
    fc = await svc.cash_forecast(session, as_of=date(2026, 6, 1), months=3)
    assert fc["opening_cash"] == 5_000_000
    proj = fc["projection"]
    # 버킷0(연체·당월=06): AP 200만 유출 → 잔액 300만 / 버킷1(2026-07): AR 300만 유입 → 잔액 600만
    assert proj[0]["label"] == "연체·당월"
    assert proj[0]["outflow"] == 2_000_000 and proj[0]["balance"] == 3_000_000
    jul = next(p for p in proj if p["label"] == "2026-07")
    assert jul["inflow"] == 3_000_000 and jul["balance"] == 6_000_000
    assert fc["min_balance"] == 3_000_000
