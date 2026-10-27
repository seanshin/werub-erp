"""검진권 운영 현황(집계) — 발급/사용/환급/정산 + 선수금 미사용 잔액."""
from datetime import date

import pytest

from erp.analytics.service import AnalyticsService
from erp.common.router import CurrentUser
from erp.errors import ForbiddenError
from erp.finance.service import VoucherLineDTO, VoucherService

svc = AnalyticsService()
_v = VoucherService()


async def _voucher(session, ref, lines, on=date(2026, 6, 1)):
    await _v.create(session, source_module="his.voucher", source_ref=ref, voucher_on=on,
                    lines=[VoucherLineDTO(*ln) for ln in lines])


async def test_voucher_overview_aggregates(session):
    # 발급 2건(선수금 Cr 2140), 사용 1건(Dr 2140), 환급 1건
    await _voucher(session, "voucher:purchase:P1",
                   [("1110", "DEBIT", 1000000), ("2140", "CREDIT", 1000000)])
    await _voucher(session, "voucher:purchase:P2",
                   [("1110", "DEBIT", 500000), ("2140", "CREDIT", 500000)])
    await _voucher(session, "voucher:redeem:R1",
                   [("2140", "DEBIT", 300000), ("4110", "CREDIT", 300000)])
    await _voucher(session, "voucher:refund:F1",
                   [("2140", "DEBIT", 100000), ("1110", "CREDIT", 100000)])
    await session.flush()
    out = await svc.voucher_overview(session, date_from=date(2026, 1, 1), date_to=date(2026, 12, 31))
    assert out["issued"] == {"amount": 1500000, "count": 2}
    assert out["redeemed"] == {"amount": 300000, "count": 1}
    assert out["refunded"] == {"amount": 100000, "count": 1}
    # 미사용 발급 잔액 = 발급 1,500,000 − 사용 300,000 − 환급 100,000 = 1,100,000
    assert out["unredeemed_krw"] == 1100000
    types = {x["line_type"] for x in out["by_line_type"]}
    assert {"purchase", "redeem", "refund"} <= types
    # 월별 추이 — 발급/사용 (모두 2026-06)
    assert out["monthly_trend"] == [{"month": "2026-06", "issued": 1500000, "redeemed": 300000}]


async def test_voucher_monthly_trend_split(session):
    """발급·사용이 다른 달이면 월별로 분리 집계."""
    await _voucher(session, "voucher:purchase:M1",
                   [("1110", "DEBIT", 200000), ("2140", "CREDIT", 200000)], on=date(2026, 3, 10))
    await _voucher(session, "voucher:redeem:M2",
                   [("2140", "DEBIT", 50000), ("4110", "CREDIT", 50000)], on=date(2026, 5, 20))
    await session.flush()
    out = await svc.voucher_overview(session, date_from=date(2026, 1, 1), date_to=date(2026, 12, 31))
    mt = {m["month"]: m for m in out["monthly_trend"]}
    assert mt["2026-03"]["issued"] == 200000 and mt["2026-03"]["redeemed"] == 0
    assert mt["2026-05"]["redeemed"] == 50000 and mt["2026-05"]["issued"] == 0


async def test_voucher_overview_date_filter(session):
    await _voucher(session, "voucher:purchase:OLD",
                   [("1110", "DEBIT", 999), ("2140", "CREDIT", 999)], on=date(2025, 1, 1))
    await session.flush()
    out = await svc.voucher_overview(session, date_from=date(2026, 1, 1), date_to=date(2026, 12, 31))
    assert out["issued"]["count"] == 0      # 2025 건은 범위 밖


async def test_voucher_status_requires_analytics_scope(session):
    from erp.analytics.router import voucher_status
    staff = CurrentUser(user_id=2, role="STAFF", dept_id=1, license_flag=True)
    with pytest.raises(ForbiddenError):
        await voucher_status(date_from=date(2026, 1, 1), date_to=date(2026, 1, 31),
                             user=staff, session=session)


async def test_voucher_unredeemed_point_in_time(session):
    """미사용잔액 as_of(date_to) 시점 고정 — 이후 발급/사용은 과거 월 잔액에 미반영(마트 소급 오염 해소)."""
    # 3월 발급 100만
    await _voucher(session, "voucher:purchase:PT1",
                   [("1110", "DEBIT", 1000000), ("2140", "CREDIT", 1000000)], on=date(2026, 3, 10))
    # 5월 사용 40만
    await _voucher(session, "voucher:redeem:PT2",
                   [("2140", "DEBIT", 400000), ("4110", "CREDIT", 400000)], on=date(2026, 5, 15))
    await session.flush()
    # 3월말 시점 — 사용 전이므로 미사용 100만
    mar = await svc.voucher_overview(session, date_from=date(2026, 1, 1), date_to=date(2026, 3, 31))
    assert mar["unredeemed_krw"] == 1000000
    # 5월말 시점 — 사용 40만 반영 → 60만
    may = await svc.voucher_overview(session, date_from=date(2026, 1, 1), date_to=date(2026, 5, 31))
    assert may["unredeemed_krw"] == 600000
