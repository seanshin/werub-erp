"""예산 워크플로 — 전용(transfer)·이월(carry-over)."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.budget_service import BudgetService
from erp.finance.models import Budget
from erp.finance.service import VoucherLineDTO, VoucherService

svc = BudgetService()
vs = VoucherService()


async def _budget(session, dept, year, acc, amt):
    await svc.amend(session, department_id=dept, fiscal_year=year, account_code=acc, new_amount=amt)


async def test_transfer_moves_between_accounts(session):
    await _budget(session, 7, 2026, "5110", 1_000_000)
    await _budget(session, 7, 2026, "5410", 200_000)
    await svc.transfer(session, department_id=7, fiscal_year=2026, from_account="5110",
                       to_account="5410", amount=300_000, reason="재료비 부족")
    src = await session.scalar(select(Budget).where(Budget.account_code == "5110"))
    dst = await session.scalar(select(Budget).where(Budget.account_code == "5410"))
    assert float(src.amount) == 700_000 and float(dst.amount) == 500_000


async def test_transfer_insufficient_blocked(session):
    await _budget(session, 7, 2026, "5110", 100_000)
    with pytest.raises(ValidationError, match="부족"):
        await svc.transfer(session, department_id=7, fiscal_year=2026, from_account="5110",
                           to_account="5410", amount=300_000)


async def test_carry_over_unused(session):
    await _budget(session, 7, 2026, "5110", 1_000_000)
    # 집행 600,000 (DR 5110 부서7)
    await vs.create(session, source_module="test", source_ref="exec1", voucher_on=date(2026, 5, 1),
                    lines=[VoucherLineDTO("5110", "DEBIT", 600_000, department_id=7),
                           VoucherLineDTO("1110", "CREDIT", 600_000)], memo="집행")
    out = await svc.carry_over(session, department_id=7, from_year=2026)
    assert out["total"] == 400_000          # 미집행 1,000,000 − 600,000
    nxt = await session.scalar(select(Budget).where(
        Budget.fiscal_year == 2027, Budget.account_code == "5110"))
    assert float(nxt.amount) == 400_000
    # 재실행 멱등 — 차년도 기존 과목 건너뜀
    out2 = await svc.carry_over(session, department_id=7, from_year=2026)
    assert out2["total"] == 0


async def test_list_encumbrances_filters_open(session):
    """약정 목록 — OPEN만·부서 필터·해제 후 제외(예산관리 화면 지원 EP)."""
    await _budget(session, 9, 2026, "5110", 1_000_000)
    e1 = await svc.encumber(session, department_id=9, fiscal_year=2026, account_code="5110",
                            amount=300_000, description="발주 예정")
    await svc.encumber(session, department_id=9, fiscal_year=2026, account_code="5110",
                       amount=100_000, description="비품")
    rows = await svc.list_encumbrances(session, fiscal_year=2026, department_id=9)
    assert len(rows) == 2 and all(r["status"] == "OPEN" for r in rows)
    assert {r["amount"] for r in rows} == {300_000.0, 100_000.0}
    # 다른 부서·연도는 제외
    assert await svc.list_encumbrances(session, fiscal_year=2026, department_id=8) == []
    assert await svc.list_encumbrances(session, fiscal_year=2025, department_id=9) == []
    # 해제 후 OPEN 목록에서 제외
    await svc.release(session, encumbrance_id=e1.id)
    rows2 = await svc.list_encumbrances(session, fiscal_year=2026, department_id=9)
    assert len(rows2) == 1 and rows2[0]["amount"] == 100_000.0
