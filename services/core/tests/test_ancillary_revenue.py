"""부대사업 수익(의료외수익·0215) — 과세 순액/매출세액 분리·면세 전액·카테고리 집계·검증."""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.errors import ValidationError
from erp.finance.ancillary_service import AncillaryRevenueService
from erp.finance.models import Account, AncillaryRevenue, Voucher, VoucherLine

svc = AncillaryRevenueService()
D = date(2026, 7, 21)


async def _accounts(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1132", name="카드미수", kind="ASSET"),
        Account(code="4130", name="의료외수익", kind="REVENUE"),
        Account(code="2135", name="부가세예수금", kind="LIABILITY")])
    await session.flush()


async def _lines(session, ref):
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == ref))
    rows = (await session.scalars(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id))).all()
    return {(x.account_code, x.side, float(x.amount)) for x in rows}


async def test_taxable_splits_net_and_vat(session):
    """과세(주차) 11,000 수납 → 순액 10,000(4130)·매출세액 1,000(2135)·현금 11,000."""
    await _accounts(session)
    item = await svc.create_item(session, name="외래주차", category="PARKING", taxable=True)
    rev = await svc.record(session, item_id=item.id, amount=11_000, method="CASH",
                           occurred_on=D)
    assert float(rev.net_amount) == 10_000 and float(rev.vat_amount) == 1_000
    assert await _lines(session, f"ancillary:{rev.id}") == {
        ("1110", "DEBIT", 11_000), ("4130", "CREDIT", 10_000), ("2135", "CREDIT", 1_000)}


async def test_record_idempotent_on_client_ref(session):
    """F5 — 동일 client_ref 재전송은 기존 수납 반환(중복 수익/전표 없음)."""
    await _accounts(session)
    item = await svc.create_item(session, name="외래주차", category="PARKING", taxable=True)
    r1 = await svc.record(session, item_id=item.id, amount=11_000, occurred_on=D,
                          client_ref="KIOSK-9x")
    r2 = await svc.record(session, item_id=item.id, amount=11_000, occurred_on=D,
                          client_ref="KIOSK-9x")     # 재전송
    assert r2.id == r1.id
    assert await session.scalar(select(func.count()).select_from(AncillaryRevenue)) == 1
    assert await session.scalar(select(func.count()).select_from(Voucher)) == 1


async def test_exempt_full_to_revenue(session):
    """면세 항목 → 전액 4130·매출세액 0·카드미수(CARD)."""
    await _accounts(session)
    item = await svc.create_item(session, name="비과세임대", category="RENTAL", taxable=False)
    rev = await svc.record(session, item_id=item.id, amount=50_000, method="CARD",
                           occurred_on=D)
    assert float(rev.net_amount) == 50_000 and float(rev.vat_amount) == 0
    assert await _lines(session, f"ancillary:{rev.id}") == {
        ("1132", "DEBIT", 50_000), ("4130", "CREDIT", 50_000)}


async def test_inactive_and_bad_input_rejected(session):
    await _accounts(session)
    item = await svc.create_item(session, name="식당", category="CAFETERIA")
    await svc.set_item_active(session, item_id=item.id, active=False)
    with pytest.raises(ValidationError, match="비활성"):
        await svc.record(session, item_id=item.id, amount=1_000, occurred_on=D)
    live = await svc.create_item(session, name="검진", category="CHECKUP")
    with pytest.raises(ValidationError, match="0보다 커야"):
        await svc.record(session, item_id=live.id, amount=0, occurred_on=D)
    with pytest.raises(ValidationError, match="분류"):
        await svc.create_item(session, name="x", category="NOPE")


async def test_summary_by_category(session):
    await _accounts(session)
    parking = await svc.create_item(session, name="주차", category="PARKING", taxable=True)
    rental = await svc.create_item(session, name="임대", category="RENTAL", taxable=False)
    await svc.record(session, item_id=parking.id, amount=11_000, occurred_on=D)
    await svc.record(session, item_id=parking.id, amount=22_000, occurred_on=D)
    await svc.record(session, item_id=rental.id, amount=100_000, occurred_on=D)
    out = await svc.summary(session, since=date(2026, 7, 1), until=date(2026, 7, 31))
    by = {c["category"]: c for c in out["by_category"]}
    assert by["PARKING"]["gross"] == 33_000 and by["PARKING"]["vat"] == 3_000
    assert by["RENTAL"]["gross"] == 100_000 and by["RENTAL"]["vat"] == 0
    assert out["total_gross"] == 133_000 and out["total_vat"] == 3_000


async def test_revenue_voucher_balances(session):
    await _accounts(session)
    item = await svc.create_item(session, name="주차", category="PARKING", taxable=True)
    rev = await svc.record(session, item_id=item.id, amount=11_000, occurred_on=D)
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == f"ancillary:{rev.id}"))
    dr = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    cr = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "CREDIT"))
    assert float(dr) == float(cr) == 11_000
    assert await session.scalar(select(func.count()).select_from(AncillaryRevenue)) == 1
