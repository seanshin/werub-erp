"""차입금 관리 — 차입 실행·월 이자 인식·원금 상환 (자금관리)."""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.errors import ValidationError
from erp.finance.loan_service import LoanService
from erp.finance.models import VoucherLine

svc = LoanService()


async def _gl(session, code):
    d, c = (await session.execute(select(
        func.coalesce(func.sum(VoucherLine.amount).filter(VoucherLine.side == "DEBIT"), 0),
        func.coalesce(func.sum(VoucherLine.amount).filter(VoucherLine.side == "CREDIT"), 0))
        .where(VoucherLine.account_code == code))).one()
    return float(d) - float(c)


async def test_loan_lifecycle(session):
    loan = await svc.register(session, lender="국민은행", principal=12_000_000,
                              annual_rate=0.06, started_on=date(2026, 1, 1), term_months=12)
    await session.flush()
    assert await _gl(session, "1110") == 12_000_000      # 현금 차변
    assert await _gl(session, "2310") == -12_000_000     # 단기차입금 대변
    # 월 이자 = 12,000,000 × 6% / 12 = 60,000
    r = await svc.accrue_interest(session, loan_id=loan.id, yyyymm="2026-01")
    assert r["interest"] == 60_000
    assert await _gl(session, "5720") == 60_000          # 이자비용 차변
    # 멱등 — 동일 월 재실행 무중복
    await svc.accrue_interest(session, loan_id=loan.id, yyyymm="2026-01")
    assert await _gl(session, "5720") == 60_000
    # 전액 상환 → REPAID
    await svc.repay(session, loan_id=loan.id, amount=12_000_000, on=date(2026, 12, 31))
    await session.flush()
    assert loan.status == "REPAID" and await _gl(session, "2310") == 0


async def test_repay_over_outstanding_blocked(session):
    loan = await svc.register(session, lender="x", principal=1_000_000, annual_rate=0.05,
                              started_on=date(2026, 1, 1), term_months=6)
    with pytest.raises(ValidationError, match="초과"):
        await svc.repay(session, loan_id=loan.id, amount=2_000_000, on=date(2026, 6, 1))


async def test_repay_same_day_same_amount_twice_both_posted(session):
    """심층 재검토 HIGH — 같은 날 동일액 2회 상환이 각각 전기(보조부=GL).

    과거 source_ref=(일자·금액)이면 2번째가 멱등 충돌로 GL 미전기·loan.repaid_principal만 이중가산
    되어 원장↔보조부가 어긋났다. source_ref에 직전 누적을 넣어 각각 유일하게 전기한다."""
    loan = await svc.register(session, lender="우리은행", principal=3_000_000, annual_rate=0.05,
                              started_on=date(2026, 1, 1), term_months=12)
    await session.flush()
    day = date(2026, 3, 10)
    await svc.repay(session, loan_id=loan.id, amount=1_000_000, on=day)
    await svc.repay(session, loan_id=loan.id, amount=1_000_000, on=day)   # 동일 날·동일액 2회
    await session.flush()
    # 보조부 2,000,000 상환 == GL. _gl=차변−대변. 2310(부채)=차입 3M(CR) − 상환 2M(DR) → −1M.
    # 상환이 1건만 전기됐던 옛 버그면 −2M이 되어 보조부(2M)와 어긋남 → 2건 모두 전기 입증.
    assert round(float(loan.repaid_principal)) == 2_000_000
    assert await _gl(session, "2310") == -1_000_000              # 잔여차입 1M(대변잔액)
    assert await _gl(session, "1110") == 3_000_000 - 2_000_000   # 현금: 차입 3M − 상환 2M = 1M
