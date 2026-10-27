"""무형자산(K-IFRS 1038)·차입원가 자본화(1023) — 취득·정액상각·자본화·멱등."""
from datetime import date

from sqlalchemy import select

from erp.finance.intangible_service import IntangibleAssetService
from erp.finance.models import Account, Voucher, VoucherLine

svc = IntangibleAssetService()


async def _seed(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="2110", name="미지급금", kind="LIABILITY"),
        Account(code="1850", name="무형자산", kind="ASSET"),
        Account(code="1855", name="무형자산상각누계", kind="ASSET"),
        Account(code="5215", name="무형자산상각비", kind="EXPENSE"),
        Account(code="1856", name="무형자산손상차손누계", kind="ASSET"),
        Account(code="5216", name="무형자산손상차손", kind="EXPENSE"),
        Account(code="4641", name="무형자산손상차손환입", kind="REVENUE"),
        Account(code="4615", name="무형자산처분이익", kind="REVENUE"),
        Account(code="5616", name="무형자산처분손실", kind="EXPENSE"),
    ])
    await session.flush()


async def test_acquire_and_amortize(session):
    await _seed(session)
    ia = await svc.create(session, asset_no="SW-1", name="EMR 소프트웨어",
                          acquired_on=date(2026, 1, 1), cost=24000000, useful_life_years=5)
    out = await svc.amortize_monthly(session, asset_id=ia["id"], period="2026-02")
    assert out["amortization"] == 400000      # 2,400만/5/12
    # 멱등
    dup = await svc.amortize_monthly(session, asset_id=ia["id"], period="2026-02")
    assert dup.get("already") is True
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == f"intangible-amort:{ia['id']}:2026-02"))
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert ("5215", "DEBIT") in sides and ("1855", "CREDIT") in sides


async def test_capitalize_borrowing(session):
    await _seed(session)
    ia = await svc.create(session, asset_no="DEV-1", name="자체개발", acquired_on=date(2026, 1, 1),
                          cost=10000000, useful_life_years=5)
    out = await svc.capitalize_borrowing(session, asset_id=ia["id"], amount=2000000,
                                         on_date=date(2026, 3, 1))
    assert out["total_capitalized"] == 2000000
    # 상각 기준에 자본화 차입원가 포함: (1000만+200만)/5/12 = 200,000
    am = await svc.amortize_monthly(session, asset_id=ia["id"], period="2026-04")
    assert am["amortization"] == 200000
    # 멱등 — 동일 자산·일자 재호출 시 보조부 이중 가산 금지(전체검토 5b)
    dup = await svc.capitalize_borrowing(session, asset_id=ia["id"], amount=2000000,
                                         on_date=date(2026, 3, 1))
    assert dup.get("already") is True and dup["total_capitalized"] == 2000000


async def test_impair_reverse_and_book_value(session):
    await _seed(session)
    ia = await svc.create(session, asset_no="SW-2", name="라이선스",
                          acquired_on=date(2026, 1, 1), cost=10000000, useful_life_years=5)
    # 장부가 1000만 → 회수가능 700만 → 손상 300만(5216/1856)
    imp = await svc.impair(session, asset_id=ia["id"], recoverable_amount=7000000,
                           on_date=date(2026, 6, 30))
    assert imp["impairment_loss"] == 3000000 and imp["book_value"] == 7000000
    # 멱등
    dup = await svc.impair(session, asset_id=ia["id"], recoverable_amount=7000000,
                           on_date=date(2026, 6, 30))
    assert dup.get("skipped")
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == f"intangible-impair:{ia['id']}:2026-06-30"))
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert ("5216", "DEBIT") in sides and ("1856", "CREDIT") in sides
    # 회복 900만 → 회수가능 초과 금지 → 환입 200만(장부가 700만→900만)
    rev = await svc.reverse_impairment(session, asset_id=ia["id"], recoverable_amount=9000000,
                                       on_date=date(2026, 9, 30))
    assert rev["reversal"] == 2000000 and rev["book_value"] == 9000000


async def test_dispose_with_gain(session):
    await _seed(session)
    ia = await svc.create(session, asset_no="SW-3", name="처분대상",
                          acquired_on=date(2026, 1, 1), cost=10000000, useful_life_years=5)
    await svc.impair(session, asset_id=ia["id"], recoverable_amount=6000000,
                     on_date=date(2026, 6, 30))   # 손상 400만 → 장부가 600만
    out = await svc.dispose(session, asset_id=ia["id"], disposed_on=date(2026, 12, 31),
                            disposal_amount=7000000)   # 처분가 700만 → 처분이익 100만
    assert out["book_value"] == 6000000 and out["gain"] == 1000000 and out["loss"] == 0
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == f"intangible-disposal:{ia['id']}"))
    lines = [(ln.account_code, ln.side, float(ln.amount)) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()]
    sides = {(c, s) for c, s, _ in lines}
    assert ("1850", "CREDIT") in sides and ("1856", "DEBIT") in sides
    assert ("1110", "DEBIT") in sides and ("4615", "CREDIT") in sides
    # 차대 균형
    deb = sum(a for _, s, a in lines if s == "DEBIT")
    cre = sum(a for _, s, a in lines if s == "CREDIT")
    assert deb == cre
    # 처분 후 재처분 불가
    try:
        await svc.dispose(session, asset_id=ia["id"], disposed_on=date(2026, 12, 31))
        raise AssertionError("이미 처분 자산 재처분이 허용됨")
    except Exception as e:  # noqa: BLE001
        assert "이미 처분" in str(e)


async def test_amortize_after_impairment_never_negative(session):
    """손상 인식 후 월상각이 손상누계를 차감해 장부가가 음수로 과다상각되지 않아야 한다
    (상각 잔여 = 원가-상각누계-손상누계, 전체검토 B)."""
    await _seed(session)
    ia = await svc.create(session, asset_no="SW-IMP", name="손상후상각",
                          acquired_on=date(2026, 1, 1), cost=1200000, useful_life_years=1)
    for m in range(1, 7):                       # 6개월 상각 → 누계 60만, 장부가 60만
        await svc.amortize_monthly(session, asset_id=ia["id"], period=f"2026-{m:02d}")
    await svc.impair(session, asset_id=ia["id"], recoverable_amount=200000,
                     on_date=date(2026, 6, 30))  # 손상 40만 → 장부가 20만
    # 잔여 20만이므로 7·8월 각 10만 상각 후 9월엔 상각완료 거부 — 장부가 음수 금지
    a7 = await svc.amortize_monthly(session, asset_id=ia["id"], period="2026-07")
    assert a7["book_value"] == 100000 and a7["book_value"] >= 0
    a8 = await svc.amortize_monthly(session, asset_id=ia["id"], period="2026-08")
    assert a8["book_value"] == 0
    try:
        await svc.amortize_monthly(session, asset_id=ia["id"], period="2026-09")
        raise AssertionError("음수 장부가까지 과다상각이 허용됨")
    except Exception as e:  # noqa: BLE001
        assert "상각 완료" in str(e)


async def test_amortize_bad_period_rejected(session):
    await _seed(session)
    ia = await svc.create(session, asset_no="SW-9", name="기간검증",
                          acquired_on=date(2026, 1, 1), cost=12000000, useful_life_years=5)
    for bad in ("abc", "2026-13", "202606", "2026-00"):
        try:
            await svc.amortize_monthly(session, asset_id=ia["id"], period=bad)
            raise AssertionError(f"잘못된 기간 통과: {bad}")
        except Exception as e:  # noqa: BLE001
            assert "YYYY-MM" in str(e)
