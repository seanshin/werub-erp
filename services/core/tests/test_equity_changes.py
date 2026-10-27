"""기본금변동계산서 (A-3) — 기초/변동/기말·재무상태표 정합."""
from datetime import date

from erp.finance.models import Account, Voucher, VoucherLine
from erp.finance.statements import StatementService

svc = StatementService()


async def _voucher(session, on, lines):
    v = Voucher(voucher_on=on, source_module="t", source_ref=f"eq:{on}:{len(lines)}")
    session.add(v)
    await session.flush()
    for code, side, amt in lines:
        session.add(VoucherLine(voucher_id=v.id, account_code=code, side=side, amount=amt))
    await session.flush()


async def test_equity_changes_with_balance_check(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="3300", name="이익잉여금", kind="CAPITAL"),
        Account(code="3400", name="재평가잉여금", kind="CAPITAL"),
        Account(code="4110", name="외래수익", kind="REVENUE"),
        Account(code="5110", name="진료비용", kind="EXPENSE"),
    ])
    # 전기(2025): 수익 1,000,000 → 결산분개로 이익잉여금 대체(현금 상대)
    await _voucher(session, date(2025, 6, 1),
                   [("1110", "DEBIT", 1_000_000), ("4110", "CREDIT", 1_000_000)])
    await _voucher(session, date(2025, 12, 31),
                   [("4110", "DEBIT", 1_000_000), ("3300", "CREDIT", 1_000_000)])
    # 당기(2026): 순이익 300,000(미결산) + 재평가잉여금 200,000 증가
    await _voucher(session, date(2026, 3, 1),
                   [("1110", "DEBIT", 500_000), ("4110", "CREDIT", 500_000)])
    await _voucher(session, date(2026, 4, 1),
                   [("5110", "DEBIT", 200_000), ("1110", "CREDIT", 200_000)])
    await _voucher(session, date(2026, 5, 1),
                   [("1110", "DEBIT", 200_000), ("3400", "CREDIT", 200_000)])

    out = await svc.equity_changes(session, fiscal_year=2026)
    by = {i["account_code"]: i for i in out["items"]}
    assert by["3300"]["opening"] == 1_000_000 and by["3300"]["closing"] == 1_000_000
    assert by["3400"]["opening"] == 0 and by["3400"]["increase"] == 200_000
    assert by["—"]["increase"] == 300_000              # 당기순이익(미처분)
    assert out["net_income"] == 300_000
    assert out["total_opening"] == 1_000_000
    assert out["total_closing"] == 1_500_000
    assert out["balanced"] is True                     # 재무상태표 equity와 양 시점 일치
