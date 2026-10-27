"""연말 결산분개 — 손익→이익잉여금·재무상태표 정합."""
from datetime import date

from erp.finance.closing_service import ClosingService
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.statements import StatementService

cs = ClosingService()
vs = VoucherService()
ss = StatementService()


async def _seed(session):
    from erp.finance.models import Account
    session.add_all([
        Account(code="4110", name="외래수익", kind="REVENUE"),
        Account(code="5310", name="급여비용", kind="EXPENSE"),
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="3300", name="이익잉여금", kind="CAPITAL")])
    await session.flush()


async def test_closing_transfers_to_retained(session):
    await _seed(session)
    # 수익 1,000 / 비용 600 (현금 상대) — 당기순이익 400
    await vs.create(session, source_module="manual", source_ref="rev",
                    voucher_on=date(2025, 6, 1),
                    lines=[VoucherLineDTO("1110", "DEBIT", 1000), VoucherLineDTO("4110", "CREDIT", 1000)])
    await vs.create(session, source_module="manual", source_ref="exp",
                    voucher_on=date(2025, 6, 2),
                    lines=[VoucherLineDTO("5310", "DEBIT", 600), VoucherLineDTO("1110", "CREDIT", 600)])
    await session.flush()
    out = await cs.close_year(session, fiscal_year=2025)
    assert out["net_income"] == 400
    await session.flush()
    # 결산 후 재무상태표: 손익 0, 이익잉여금 400, 정합
    bs = await ss.balance_sheet(session, as_of=date(2025, 12, 31))
    assert bs["capital_total"] == 400 and round(bs["retained_earnings"]) == 0
    assert bs["balanced"]


async def test_closing_idempotent(session):
    await _seed(session)
    await vs.create(session, source_module="manual", source_ref="r2",
                    voucher_on=date(2025, 3, 1),
                    lines=[VoucherLineDTO("1110", "DEBIT", 500), VoucherLineDTO("4110", "CREDIT", 500)])
    await session.flush()
    o1 = await cs.close_year(session, fiscal_year=2025)
    o2 = await cs.close_year(session, fiscal_year=2025)
    assert o1["voucher_id"] == o2["voucher_id"]   # 멱등(closing:2025)


async def test_closing_includes_reversed_original_in_its_year(session):
    """심층 재검토 HIGH — 손익계정이 역분개돼도 원 전표 연도 결산은 그 손익을 인식.

    역분개는 개방기간(오늘=차년도)으로 전기되므로 원 수익은 발생 연도(2025)에 남는다. 과거 close_year가
    POSTED만 집계해 REVERSED 원전표를 탈락시키면 당기순이익·이익잉여금이 과소계상됐다(손익계산서와 불일치)."""
    await _seed(session)
    v = await vs.create(session, source_module="manual", source_ref="rev-2025",
                        voucher_on=date(2025, 6, 1),
                        lines=[VoucherLineDTO("1110", "DEBIT", 1000),
                               VoucherLineDTO("4110", "CREDIT", 1000)])
    await vs.reverse(session, voucher_id=v.id, reason="정정")   # 역분개 = 오늘(차년도) 전기
    await session.flush()
    # 2025 손익계산서(POSTED+REVERSED·연도 필터)는 수익 1000 → 결산도 동일해야 정합
    assert (await ss.income_statement(session, since=date(2025, 1, 1),
                                      until=date(2025, 12, 31)))["total_revenue"] == 1000
    out = await cs.close_year(session, fiscal_year=2025)
    assert out["total_revenue"] == 1000 and out["net_income"] == 1000


async def test_income_statement_excludes_closing(session):
    """P0-6 — 결산분개 후에도 손익계산서 수익 유지(closing 제외, 신고자료 0 붕괴 방지)."""
    await _seed(session)
    await vs.create(session, source_module="manual", source_ref="rev",
                    voucher_on=date(2025, 6, 1),
                    lines=[VoucherLineDTO("1110", "DEBIT", 1000), VoucherLineDTO("4110", "CREDIT", 1000)])
    await session.flush()
    period = dict(since=date(2025, 1, 1), until=date(2025, 12, 31))
    assert (await ss.income_statement(session, **period))["total_revenue"] == 1000
    await cs.close_year(session, fiscal_year=2025)
    await session.flush()
    # 결산 후에도 손익계산서·월별손익 수익 유지(closing 제외)
    assert (await ss.income_statement(session, **period))["total_revenue"] == 1000
    assert sum(m["revenue"] for m in await ss.monthly_pl(session, year=2025)) == 1000
    # 재무상태표는 closing 포함 → 이익잉여금 1000, 정합
    bs = await ss.balance_sheet(session, as_of=date(2025, 12, 31))
    assert bs["capital_total"] == 1000 and bs["balanced"]
