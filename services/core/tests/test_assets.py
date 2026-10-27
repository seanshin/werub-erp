"""고정자산·감가상각 (FR-09 확장) — 취득전표·정액상각·멱등·재무제표 연동·처분."""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.errors import ValidationError
from erp.finance.asset_service import AssetService
from erp.finance.models import Account, DepreciationEntry, FixedAsset, Voucher
from erp.finance.statements import StatementService

svc = AssetService()
ss = StatementService()


async def _accounts(session):
    session.add_all([
        Account(code="1710", name="의료장비", kind="ASSET"),
        Account(code="1810", name="감가상각누계액", kind="ASSET"),
        Account(code="2110", name="미지급금", kind="LIABILITY"),
        Account(code="5210", name="감가상각비", kind="EXPENSE"),
    ])
    await session.flush()


async def _mri(session):
    return await svc.register(session, asset_no="EQ-001", name="MRI", category="MEDICAL",
                              acquired_on=date(2026, 1, 1), acquisition_cost=12000000,
                              useful_life_years=5)


async def test_register_creates_acquisition_voucher(session):
    await _accounts(session)
    a = await _mri(session)
    assert a.id and a.status == "IN_USE"
    v = await session.scalar(select(func.count()).select_from(Voucher)
                             .where(Voucher.source_ref == "asset-acq-EQ-001"))
    assert v == 1  # 취득 전표(의료장비/미지급)


async def test_straight_line_depreciation(session):
    await _accounts(session)
    await _mri(session)
    n = await svc.run_depreciation(session, period="2026-01")
    assert n == 1
    e = (await session.execute(select(DepreciationEntry))).scalar_one()
    assert e.amount == 200000          # 12,000,000 / (5×12)
    assert e.book_value_after == 11800000
    a = (await session.execute(select(FixedAsset))).scalar_one()
    assert a.accumulated_depreciation == 200000


async def test_depreciation_idempotent(session):
    await _accounts(session)
    await _mri(session)
    await svc.run_depreciation(session, period="2026-01")
    assert await svc.run_depreciation(session, period="2026-01") == 0   # 재실행 무효
    cnt = await session.scalar(select(func.count()).select_from(DepreciationEntry))
    assert cnt == 1


async def test_depreciation_flows_to_statements(session):
    await _accounts(session)
    await _mri(session)
    await svc.run_depreciation(session, period="2026-01")
    inc = await ss.income_statement(session, since=date(2026, 1, 1), until=date(2026, 1, 31))
    assert inc["total_expense"] == 200000               # 감가상각비
    bs = await ss.balance_sheet(session, as_of=date(2026, 1, 31))
    assert bs["total_assets"] == 11800000               # 의료장비 12M − 누계 200k
    assert bs["total_liabilities"] == 12000000          # 미지급
    assert bs["retained_earnings"] == -200000           # 감가비만큼 손실


async def test_dispose_with_gain(session):
    """처분가 > 장부가 → 처분이익, 전표 차대 균형."""
    await _accounts(session)
    a = await _mri(session)                       # 12M, 5yr
    await svc.run_depreciation(session, period="2026-01")   # 누계 200k, 장부 11.8M
    out = await svc.dispose(session, asset_id=a.id, disposed_on=date(2026, 2, 1),
                            disposal_amount=12000000)
    assert out["status"] == "DISPOSED"
    assert out["book_value"] == 11800000 and out["gain_loss"] == 200000   # 이익


async def test_dispose_with_loss(session):
    await _accounts(session)
    a = await _mri(session)
    await svc.run_depreciation(session, period="2026-01")
    out = await svc.dispose(session, asset_id=a.id, disposed_on=date(2026, 2, 1),
                            disposal_amount=10000000)
    assert out["gain_loss"] == -1800000          # 손실 (11.8M − 10M)


async def test_dispose_catches_up_missed_depreciation(session):
    """감가처분일 catch-up — Jan만 상각된 자산을 5/31 처분 시 Feb~May 감가를 채워(월말≤처분일)
    처분손익을 현행 장부가로 산출(IAS 16.55·타임라인 정합)."""
    await _accounts(session)
    a = await _mri(session)                                  # 12M, 5yr, 월 200k, 취득 2026-01
    await svc.run_depreciation(session, period="2026-01")   # Jan만 상각(누계 200k)
    out = await svc.dispose(session, asset_id=a.id, disposed_on=date(2026, 5, 31),
                            disposal_amount=11_000_000)
    entries = await session.scalar(select(func.count()).select_from(DepreciationEntry)
                                   .where(DepreciationEntry.asset_id == a.id))
    assert entries == 5                                      # Jan + Feb~May 자동
    assert float(a.accumulated_depreciation) == 1_000_000   # 5개월 × 200k
    assert out["book_value"] == 11_000_000 and out["gain_loss"] == 0


async def test_dispose_early_in_month_excludes_that_month(session):
    """월배치와 동일한 월말 인식 — 월 초 처분은 미완료 월 감가 없음(2/1 처분 → 2월 감가 제외)."""
    await _accounts(session)
    a = await _mri(session)
    await svc.run_depreciation(session, period="2026-01")   # 누계 200k
    out = await svc.dispose(session, asset_id=a.id, disposed_on=date(2026, 2, 1),
                            disposal_amount=11_800_000)
    entries = await session.scalar(select(func.count()).select_from(DepreciationEntry)
                                   .where(DepreciationEntry.asset_id == a.id))
    assert entries == 1                                      # Feb 미완료 → 미상각
    assert out["book_value"] == 11_800_000 and out["gain_loss"] == 0


async def test_impair_catches_up_depreciation_first(session):
    """손상 측정 전 감가 현행화 — 밀린 감가 위에서 손상을 측정하면 과대 손상(1.8M)이나, 현행화 후
    현행 장부가(11M)로 측정 → 손상 1M. 이후 처분 시 밀린 감가 소급으로 인한 이중계상 원천 차단."""
    await _accounts(session)
    session.add_all([Account(code="1815", name="손상누계", kind="ASSET"),
                     Account(code="5620", name="손상차손", kind="EXPENSE")])
    await session.flush()
    a = await _mri(session)
    await svc.run_depreciation(session, period="2026-01")   # 누계 200k(밀림)
    out = await svc.impair(session, asset_id=a.id, recoverable_amount=10_000_000,
                           on_date=date(2026, 5, 31))
    assert float(a.accumulated_depreciation) == 1_000_000   # Feb~May 현행화
    assert out["impairment_loss"] == 1_000_000              # 11M − 10M (과대 1.8M 아님)


async def test_manual_accum_asset_skips_catchup(session):
    """스케줄(엔트리) 없는 수동/이관 자산은 stored 누계를 SoT로 보존(catch-up 미적용·기존 동작)."""
    await _accounts(session)
    a = FixedAsset(asset_no="EQ-LEG", name="이관장비", category="MEDICAL",
                   acquired_on=date(2024, 1, 1), acquisition_cost=12_000_000,
                   useful_life_years=5, accumulated_depreciation=2_000_000)
    session.add(a)
    await session.flush()
    out = await svc.dispose(session, asset_id=a.id, disposed_on=date(2026, 7, 1),
                            disposal_amount=10_000_000)
    entries = await session.scalar(select(func.count()).select_from(DepreciationEntry)
                                   .where(DepreciationEntry.asset_id == a.id))
    assert entries == 0                                      # 유도 상각 없음
    assert out["book_value"] == 10_000_000 and out["gain_loss"] == 0   # stored 누계 그대로


async def test_dispose_twice_rejected(session):
    await _accounts(session)
    a = await _mri(session)
    await svc.dispose(session, asset_id=a.id, disposed_on=date(2026, 2, 1))
    with pytest.raises(ValidationError, match="이미 처분"):
        await svc.dispose(session, asset_id=a.id, disposed_on=date(2026, 3, 1))


async def test_register_cash_payment(session):
    """현금 취득 — 전표 대변이 현금(균형 검증)."""
    await _accounts(session)
    a = await svc.register(session, asset_no="EQ-C", name="현금장비", category="MEDICAL",
                           acquired_on=date(2026, 1, 1), acquisition_cost=5000000,
                           useful_life_years=5, payment_method="CASH")
    assert a.id    # 균형 안 맞으면 VoucherService가 예외


async def test_declining_decreasing_and_deterministic(session):
    """정률법 — 월 상각액이 체감(장부가 감소). 경과기간 기반 결정론."""
    await _accounts(session)
    await svc.register(session, asset_no="EQ-D", name="정률장비", category="MEDICAL",
                       acquired_on=date(2026, 1, 1), acquisition_cost=10000000,
                       useful_life_years=5, salvage_value=1000000, method="DECLINING")
    for p in ("2026-01", "2026-02", "2026-03"):
        await svc.run_depreciation(session, period=p)
    es = (await session.execute(
        select(DepreciationEntry).order_by(DepreciationEntry.period))).scalars().all()
    assert es[0].amount > es[1].amount > es[2].amount > 0    # 체감


async def test_salvage_not_over_depreciated(session):
    await _accounts(session)
    # 취득가 120k, 잔존 0, 1년 → 월 10k, 13개월 돌려도 누계 ≤ 120k
    await svc.register(session, asset_no="EQ-9", name="소형장비", category="MEDICAL",
                       acquired_on=date(2026, 1, 1), acquisition_cost=120000,
                       useful_life_years=1)
    for p in [f"2026-{m:02d}" for m in range(1, 13)] + ["2027-01"]:
        await svc.run_depreciation(session, period=p)
    a = (await session.execute(
        select(FixedAsset).where(FixedAsset.asset_no == "EQ-9"))).scalar_one()
    assert a.accumulated_depreciation == 120000          # 잔존가(0) 미만 상각 안 됨


async def test_depreciation_stamps_cost_center_from_department(session):
    """G9 자동채움 — 감가상각 전기 라인에 자산 소유부서 코드가 cost_center로 스탬프된다."""
    from erp.common.models import Department
    from erp.finance.asset_service import ACC_DEP_EXP
    from erp.finance.models import VoucherLine
    await _accounts(session)
    dept = Department(name="영상의학과", code="RA")
    session.add(dept)
    await session.flush()
    await svc.register(session, asset_no="CT-1", name="CT", category="MEDICAL",
                       acquired_on=date(2026, 1, 1), acquisition_cost=12_000_000,
                       useful_life_years=5, department_id=dept.id)
    # 취득 전표(자산 debit)에도 cost_center 스탬프
    from erp.finance.asset_service import ACC_EQUIP
    acq = (await session.execute(select(VoucherLine).where(
        VoucherLine.account_code == ACC_EQUIP))).scalars().first()
    assert acq is not None and acq.cost_center == "RA"
    # 감가 전기(비용) cost_center
    await svc.run_depreciation(session, period="2026-02")
    exp = (await session.execute(select(VoucherLine).where(
        VoucherLine.account_code == ACC_DEP_EXP))).scalars().first()
    assert exp is not None and exp.cost_center == "RA"
