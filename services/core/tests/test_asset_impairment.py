"""고정자산 손상차손 — 장부가액 차감·전표·처분 반영."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.asset_service import AssetService
from erp.finance.models import Account, FixedAsset, Voucher, VoucherLine

svc = AssetService()


async def _seed(session, cost=10_000_000, accum=2_000_000):
    session.add_all([
        Account(code="1710", name="의료장비", kind="ASSET"),
        Account(code="1810", name="감가상각누계액", kind="ASSET"),
        Account(code="1815", name="손상차손누계액", kind="ASSET"),
        Account(code="5620", name="유형자산손상차손", kind="EXPENSE"),
        Account(code="5610", name="유형자산처분손실", kind="EXPENSE"),
        Account(code="4610", name="유형자산처분이익", kind="REVENUE"),
        Account(code="1110", name="현금", kind="ASSET")])
    a = FixedAsset(asset_no="EQ-1", name="CT", category="MEDICAL", acquired_on=date(2024, 1, 1),
                   acquisition_cost=cost, useful_life_years=5, accumulated_depreciation=accum)
    session.add(a)
    await session.flush()
    return a


async def test_impair_reduces_book_and_posts_voucher(session):
    a = await _seed(session)   # book = 10M - 2M = 8M
    out = await svc.impair(session, asset_id=a.id, recoverable_amount=5_000_000, on_date=date(2026, 6, 1))
    assert out["impairment_loss"] == 3_000_000 and out["book_after"] == 5_000_000
    assert float(a.accumulated_impairment) == 3_000_000
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref.like("asset-impair-EQ-1%")))
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert ("5620", "DEBIT") in sides and ("1815", "CREDIT") in sides


async def test_impair_rejected_if_recoverable_ge_book(session):
    a = await _seed(session)
    with pytest.raises(ValidationError, match="손상 없음"):
        await svc.impair(session, asset_id=a.id, recoverable_amount=9_000_000, on_date=date(2026, 6, 1))


async def test_dispose_reverses_impairment(session):
    a = await _seed(session)
    await svc.impair(session, asset_id=a.id, recoverable_amount=5_000_000, on_date=date(2026, 6, 1))
    await session.flush()
    # 장부 5M 자산을 5M에 처분 → 손익 0
    out = await svc.dispose(session, asset_id=a.id, disposed_on=date(2026, 7, 1),
                            disposal_amount=5_000_000)
    assert out["book_value"] == 5_000_000 and out["gain_loss"] == 0


async def test_revalue_up_creates_surplus(session):
    a = await _seed(session)   # book = 10M - 2M = 8M
    from erp.finance.models import Account
    session.add(Account(code="3400", name="재평가잉여금", kind="CAPITAL"))
    session.add(Account(code="3300", name="이익잉여금", kind="CAPITAL"))
    await session.flush()
    out = await svc.revalue(session, asset_id=a.id, fair_value=10_000_000, on_date=date(2026, 6, 1))
    assert out["delta"] == 2_000_000 and out["book_after"] == 10_000_000   # 8M→10M
    assert float(a.accumulated_revaluation) == 2_000_000
    v = await session.scalar(select(Voucher).where(Voucher.source_ref.like("asset-reval-EQ-1%")))
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert ("1710", "DEBIT") in sides and ("3400", "CREDIT") in sides


async def test_impair_idempotent_same_date(session):
    """P1-8 — 동일 자산·일자 재손상은 멱등(누계 이중증가·전표 중복 방지)."""
    a = await _seed(session)   # book 8M
    await svc.impair(session, asset_id=a.id, recoverable_amount=5_000_000, on_date=date(2026, 6, 1))
    assert float(a.accumulated_impairment) == 3_000_000
    out2 = await svc.impair(session, asset_id=a.id, recoverable_amount=5_000_000, on_date=date(2026, 6, 1))
    assert "skipped" in out2                                  # 재호출 멱등
    assert float(a.accumulated_impairment) == 3_000_000      # 누계 불변(이중증가 없음)


async def test_revalue_idempotent_same_date(session):
    """P1-8 — 동일 자산·일자 재평가 멱등(잉여금 이중반영 방지)."""
    a = await _seed(session)   # book 8M
    await svc.revalue(session, asset_id=a.id, fair_value=9_000_000, on_date=date(2026, 6, 1))
    assert float(a.accumulated_revaluation) == 1_000_000
    out2 = await svc.revalue(session, asset_id=a.id, fair_value=9_000_000, on_date=date(2026, 6, 1))
    assert "skipped" in out2 and float(a.accumulated_revaluation) == 1_000_000


async def test_reverse_impairment(session):
    """손상 환입(1036) — 회수가능액 회복 시 손상누계 한도 내 환입(수익)."""
    a = await _seed(session)   # book = 8M
    session.add(Account(code="4640", name="유형자산손상차손환입", kind="REVENUE"))
    await session.flush()
    await svc.impair(session, asset_id=a.id, recoverable_amount=5_000_000, on_date=date(2026, 6, 1))
    # book 5M, 손상누계 3M. 회수가능액 7M → 환입 = min(7M-5M, 3M) = 2M
    out = await svc.reverse_impairment(session, asset_id=a.id, recoverable_amount=7_000_000,
                                       on_date=date(2026, 9, 1))
    assert out["reversal"] == 2_000_000 and out["book_after"] == 7_000_000
    assert float(a.accumulated_impairment) == 1_000_000   # 3M - 2M
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref.like("asset-impair-reverse-EQ-1%")))
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert ("1815", "DEBIT") in sides and ("4640", "CREDIT") in sides


async def test_reverse_capped_at_accum_impairment(session):
    """환입 한도 — 손상누계 초과 불가(원가모형)."""
    a = await _seed(session)
    session.add(Account(code="4640", name="유형자산손상차손환입", kind="REVENUE"))
    await session.flush()
    await svc.impair(session, asset_id=a.id, recoverable_amount=5_000_000, on_date=date(2026, 6, 1))
    # 회수가능액 20M로 과대해도 환입은 손상누계 3M까지만 → book 8M 복원
    out = await svc.reverse_impairment(session, asset_id=a.id, recoverable_amount=20_000_000,
                                       on_date=date(2026, 9, 1))
    assert out["reversal"] == 3_000_000 and out["book_after"] == 8_000_000
    assert float(a.accumulated_impairment) == 0


async def test_revaluation_model_impairment_oci(session):
    """재평가모형 자산 — 손상은 재평가잉여금(OCI) 우선 차감, 환입도 OCI."""
    a = await _seed(session)   # cost 10M, accum_dep 2M
    session.add_all([Account(code="3400", name="재평가잉여금", kind="CAPITAL"),
                     Account(code="4640", name="유형자산손상차손환입", kind="REVENUE")])
    a.accumulated_revaluation = 3000000
    a.fair_value_level = "2"
    await session.flush()
    # book = 10M + 3M - 2M = 11M, 회수 8M → 손상 3M: 전액 재평가잉여금(OCI) 차감
    out = await svc.impair(session, asset_id=a.id, recoverable_amount=8000000, on_date=date(2026, 6, 1))
    assert out["impairment_loss"] == 3000000 and out["oci_portion"] == 3000000 and out["pl_portion"] == 0
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref.like("asset-impair-EQ-1%"), Voucher.source_module == "asset"))
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert ("3400", "DEBIT") in sides and ("1815", "CREDIT") in sides
    rev = await svc.reverse_impairment(session, asset_id=a.id, recoverable_amount=11000000,
                                       on_date=date(2026, 9, 1))
    assert rev["to_oci"] is True and rev["reversal"] == 3000000
