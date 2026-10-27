"""대손충당금 / ECL (K-IFRS 1109·1037) — 연령버킷 충당 산출·차액 결산분개·멱등·환입."""
from datetime import date

from erp.finance.allowance_service import AllowanceService
from erp.finance.models import Account, AllowanceRate, Receivable

svc = AllowanceService()


async def _seed_rates(session):
    session.add_all([
        Account(code="1130", name="미수금", kind="ASSET"),
        Account(code="1190", name="대손충당금", kind="ASSET"),
        Account(code="5690", name="대손상각비", kind="EXPENSE"),
        AllowanceRate(kind="AR", bucket="current", rate=0.005),
        AllowanceRate(kind="AR", bucket="1-30", rate=0.02),
        AllowanceRate(kind="AR", bucket="31-60", rate=0.10),
        AllowanceRate(kind="AR", bucket="61-90", rate=0.30),
        AllowanceRate(kind="AR", bucket="90+", rate=0.50),
    ])
    await session.flush()


async def test_preview_computes_ecl(session):
    await _seed_rates(session)
    # as_of=2026-05-31 기준: r1 due 2026-04-30 → 31일 경과(31-60 버킷) / r2 → 90+ 버킷
    session.add_all([
        Receivable(party="보험자", amount=1000000, collected_amount=0,
                   billed_on=date(2026, 4, 1), due_on=date(2026, 4, 30), status="OUTSTANDING"),
        Receivable(party="환자", amount=500000, collected_amount=0,
                   billed_on=date(2025, 11, 1), due_on=date(2025, 11, 13), status="OUTSTANDING"),
    ])
    await session.flush()
    p = await svc.preview(session, yyyymm="2026-05")
    # 31-60: 100만×10%=10만 / 90+: 50만×50%=25만 → 35만
    assert p["required"] == 350000
    assert p["current"] == 0 and p["delta"] == 350000


async def test_post_idempotent_and_reversal(session):
    await _seed_rates(session)
    session.add(Receivable(party="보험자", amount=1000000, collected_amount=0,
                           billed_on=date(2025, 11, 1), due_on=date(2025, 11, 13),
                           status="OUTSTANDING"))
    await session.flush()
    r1 = await svc.post(session, yyyymm="2026-05")          # 90+ 100만×50%=50만 전입
    assert r1["posted"] and r1["delta"] == 500000
    # 재실행 — 충당 반영 후 delta 0(멱등)
    r2 = await svc.post(session, yyyymm="2026-05")
    assert r2["posted"] is False and r2["delta"] == 0
    # 충당잔액 = 50만(대변)
    bal = await svc._allowance_balance(session, as_of=date(2026, 5, 31))
    assert bal == 500000


async def test_forward_looking_overlay(session):
    """forward-looking ECL — 오버레이 계수로 목표충당 가중(IFRS 9)."""
    await _seed_rates(session)
    session.add(Receivable(party="보험자", amount=1000000, collected_amount=0,
                           billed_on=date(2025, 11, 1), due_on=date(2025, 11, 13),
                           status="OUTSTANDING"))   # 90+ 100만×50% = base 50만
    await session.flush()
    p0 = await svc.preview(session, yyyymm="2026-05")
    assert p0["base_ecl"] == 500000 and p0["overlay"] == 1.0 and p0["required"] == 500000
    # 경기악화 오버레이 1.2 → 목표충당 60만
    await svc.set_overlay(session, factor=1.2)
    p1 = await svc.preview(session, yyyymm="2026-05")
    assert p1["overlay"] == 1.2 and p1["required"] == 600000
    import pytest as _pt
    with _pt.raises(ValueError):
        await svc.set_overlay(session, factor=10)


async def test_overlay_does_not_collapse_default_rates(session):
    """버킷요율 미시드(코드 기본값 폴백) 상태에서 overlay를 설정해도 요율이 0으로
    무너지지 않아야 한다 — rates()가 OVERLAY 버킷을 연령요율로 오인하던 회귀(전체검토 A)."""
    session.add_all([
        Account(code="1130", name="미수금", kind="ASSET"),
        Account(code="1190", name="대손충당금", kind="ASSET"),
        Account(code="5690", name="대손상각비", kind="EXPENSE"),
    ])  # 버킷요율은 시드하지 않음 → _DEFAULT_RATES 폴백 사용
    session.add(Receivable(party="보험자", amount=1000000, collected_amount=0,
                           billed_on=date(2025, 11, 1), due_on=date(2025, 11, 13),
                           status="OUTSTANDING"))  # 90+ 100만 × 기본 50% = 50만
    await session.flush()
    await svc.set_overlay(session, factor=1.2)
    p = await svc.preview(session, yyyymm="2026-05")
    assert p["base_ecl"] == 500000          # 폴백 요율 유효 (0으로 붕괴 아님)
    assert p["required"] == 600000          # 50만 × 1.2 오버레이
    r = await svc.post(session, yyyymm="2026-05")
    assert r["posted"] and r["delta"] == 600000   # 전입(환입 아님)
