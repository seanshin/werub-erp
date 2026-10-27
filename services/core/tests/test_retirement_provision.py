"""퇴직급여충당부채 — 월별 적립·전표·멱등."""
from datetime import date

from sqlalchemy import select

from erp.finance.models import Account, Voucher, VoucherLine
from erp.hr.models import Employee, PayrollLine, PayrollRecord
from erp.hr.retirement_service import RetirementService

svc = RetirementService()


async def _seed(session):
    session.add_all([
        Account(code="2120", name="퇴직급여충당부채", kind="LIABILITY"),
        Account(code="5320", name="퇴직급여비용", kind="EXPENSE")])
    e = Employee(employee_no="R-1", name="근속자", hired_on=date(2022, 1, 1))
    session.add(e)
    await session.flush()
    rec = PayrollRecord(employee_id=e.id, yyyymm="2026-05")
    session.add(rec)
    await session.flush()
    session.add(PayrollLine(record_id=rec.id, item_kind="EARNING", item_code="BASE", amount=3_000_000))
    await session.flush()
    return e


async def test_accrue_creates_provision_and_voucher(session):
    await _seed(session)
    out = await svc.accrue_monthly(session, period="2026-05")
    assert out["employees"] == 1 and out["total_increment"] > 0
    await session.flush()
    # 전표: 5320 DEBIT / 2120 CREDIT
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "retire:2026-05"))
    assert v is not None
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert ("5320", "DEBIT") in sides and ("2120", "CREDIT") in sides
    prov = await svc.provisions(session, period="2026-05")
    assert prov["items"][0]["estimate"] > 0


async def test_accrue_increment_only_next_month(session):
    await _seed(session)
    await svc.accrue_monthly(session, period="2026-05")
    await session.flush()
    await svc.accrue_monthly(session, period="2026-06")   # 1개월 추가 근속분만 전입
    await session.flush()
    p1 = (await svc.provisions(session, period="2026-05"))["items"][0]
    p2 = (await svc.provisions(session, period="2026-06"))["items"][0]
    assert p2["cumulative"] >= p1["cumulative"]
    assert round(p2["increment"]) == round(p2["cumulative"] - p1["cumulative"])


async def test_dc_pension_contribution(session):
    from datetime import date
    from sqlalchemy import select
    from erp.finance.models import Account, Voucher, VoucherLine
    from erp.hr.models import Employee, PayrollLine, PayrollRecord
    session.add_all([Account(code="2120", name="퇴직급여충당부채", kind="LIABILITY"),
                     Account(code="2110", name="미지급금", kind="LIABILITY"),
                     Account(code="5320", name="퇴직급여비용", kind="EXPENSE")])
    e = Employee(employee_no="DC-1", name="DC직원", hired_on=date(2022, 1, 1), pension_type="DC")
    session.add(e)
    await session.flush()
    rec = PayrollRecord(employee_id=e.id, yyyymm="2026-05")
    session.add(rec)
    await session.flush()
    session.add(PayrollLine(record_id=rec.id, item_kind="EARNING", item_code="BASE", amount=3_600_000))
    await session.flush()
    out = await svc.accrue_monthly(session, period="2026-05")
    assert out["dc_contribution"] == 300_000 and out["db_provision"] == 0   # 360만/12
    await session.flush()
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "retire:2026-05"))
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert ("5320", "DEBIT") in sides and ("2110", "CREDIT") in sides   # DC → 미지급금
