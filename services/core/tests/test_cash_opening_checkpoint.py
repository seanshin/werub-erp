"""자금일보·현금흐름투영 개시잔액 체크포인트 서빙(성능 감사 HIGH) — 체크포인트+델타 == 전구간 스캔.

daily_cash/cash_forecast 개시잔액이 개시이래 현금라인 전건 재집계였던 갭. `_by_account_as_of`
(체크포인트+당기 델타)로 서빙 전환. 경계를 사이에 둔 전표로 (a)체크포인트 미적재 폴백 (b)적재 후
델타 합산이 동일 값을 내는지 검증."""
from datetime import date

from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.settlement_service import SettlementService
from erp.finance.statements import StatementService

_vs = VoucherService()
_settle = SettlementService()
_stmt = StatementService()


async def _seed_cash(session):
    """현금(1110) 순증: 1/10 +100k · 3/15 +50k−20k · 6/1 +40k → 6/30 누적 170k."""
    await _vs.create(session, source_module="patient", source_ref="co-1",
                     voucher_on=date(2026, 1, 10),
                     lines=[VoucherLineDTO("1110", "DEBIT", 100_000),
                            VoucherLineDTO("4110", "CREDIT", 100_000)])
    await _vs.create(session, source_module="patient", source_ref="co-2",
                     voucher_on=date(2026, 3, 15),
                     lines=[VoucherLineDTO("1110", "DEBIT", 50_000),
                            VoucherLineDTO("4110", "CREDIT", 50_000)])
    await _vs.create(session, source_module="scm", source_ref="co-3",
                     voucher_on=date(2026, 3, 15),
                     lines=[VoucherLineDTO("2110", "DEBIT", 20_000),
                            VoucherLineDTO("1110", "CREDIT", 20_000)])
    await _vs.create(session, source_module="patient", source_ref="co-4",
                     voucher_on=date(2026, 6, 1),
                     lines=[VoucherLineDTO("1110", "DEBIT", 40_000),
                            VoucherLineDTO("4110", "CREDIT", 40_000)])


async def test_cash_opening_fallback_equals_expected(session):
    """체크포인트 미적재 → 전구간 폴백. 7/1 개시(6/30까지) = 170k."""
    await _seed_cash(session)
    op = await _settle._cash_opening(session, cash_accts={"1110"}, until=date(2026, 6, 30))
    assert op.get("1110") == 170_000


async def test_cash_opening_checkpoint_equals_fullscan(session):
    """3/31 체크포인트 적재 후에도 델타(4~6월) 합산 == 전구간 값(170k). 서빙 동등성."""
    await _seed_cash(session)
    before = await _settle._cash_opening(session, cash_accts={"1110"}, until=date(2026, 6, 30))
    await _stmt.refresh_balance_checkpoint(session, through=date(2026, 3, 31))
    after = await _settle._cash_opening(session, cash_accts={"1110"}, until=date(2026, 6, 30))
    assert after.get("1110") == before.get("1110") == 170_000
    # 경계 당일(3/31) 조회 = 체크포인트 값(130k), 델타 없음
    at_cp = await _settle._cash_opening(session, cash_accts={"1110"}, until=date(2026, 3, 31))
    assert at_cp.get("1110") == 130_000


async def test_daily_cash_opening_uses_checkpoint(session):
    """자금일보 기초잔액이 체크포인트 서빙 후에도 정확(전일까지)."""
    await _seed_cash(session)
    await _stmt.refresh_balance_checkpoint(session, through=date(2026, 3, 31))
    # 7/1 당일 유입 5k
    await _vs.create(session, source_module="patient", source_ref="co-day",
                     voucher_on=date(2026, 7, 1),
                     lines=[VoucherLineDTO("1110", "DEBIT", 5_000),
                            VoucherLineDTO("4110", "CREDIT", 5_000)])
    out = await _settle.daily_cash(session, on=date(2026, 7, 1))
    assert out["opening"] == 170_000        # 6/30까지(당일 제외)
    assert out["inflow"] == 5_000
    assert out["closing"] == 175_000


async def test_cash_forecast_opening_uses_checkpoint(session):
    """현금흐름투영 기초잔액이 체크포인트 서빙 후에도 정확(as_of까지 포함)."""
    await _seed_cash(session)
    await _stmt.refresh_balance_checkpoint(session, through=date(2026, 3, 31))
    out = await _settle.cash_forecast(session, as_of=date(2026, 6, 30), months=3)
    assert out["opening_cash"] == 170_000
