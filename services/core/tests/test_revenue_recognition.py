"""수익인식 발생주의 (K-IFRS 1115) — 미청구수익 발생·차기 환입·멱등."""
import pytest

from erp.finance.models import Account, Voucher, VoucherLine
from erp.finance.revenue_recognition_service import RevenueRecognitionService
from sqlalchemy import select

svc = RevenueRecognitionService()


async def _seed(session):
    session.add_all([
        Account(code="1135", name="계약자산(미청구수익)", kind="ASSET"),
        Account(code="4110", name="의료수익", kind="REVENUE"),
    ])
    await session.flush()


async def test_accrue_and_reverse(session):
    await _seed(session)
    out = await svc.accrue_unbilled(session, yyyymm="2026-05", amount=3000000)
    assert out["amount"] == 3000000
    # 발생(5/31): 1135 차변 / 4110 대변
    v1 = await session.get(Voucher, out["accrual_voucher"])
    assert str(v1.voucher_on) == "2026-05-31"
    sides1 = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v1.id))).scalars()}
    assert ("1135", "DEBIT") in sides1 and ("4110", "CREDIT") in sides1
    # 환입(6/1): 반대
    v2 = await session.get(Voucher, out["reversal_voucher"])
    assert str(v2.voucher_on) == "2026-06-01"
    sides2 = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v2.id))).scalars()}
    assert ("4110", "DEBIT") in sides2 and ("1135", "CREDIT") in sides2


async def test_idempotent_and_validation(session):
    await _seed(session)
    a = await svc.accrue_unbilled(session, yyyymm="2026-05", amount=1000000)
    b = await svc.accrue_unbilled(session, yyyymm="2026-05", amount=1000000)
    assert a["accrual_voucher"] == b["accrual_voucher"]   # 멱등(동일 전표)
    assert (await svc.status(session, yyyymm="2026-05"))["accrued"] is True
    with pytest.raises(ValueError):
        await svc.accrue_unbilled(session, yyyymm="2026-07", amount=0)
