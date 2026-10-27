"""v1.305 — 정규 지급 없이 공제만 있는 급여(성과급만 있는 달의 소급 공제 등)도 확정 시 전표(DR 미지급금 / CR 예수금)."""
from datetime import date

from sqlalchemy import select

from erp.finance.models import Voucher, VoucherLine
from erp.hr.employee_service import PayrollService
from erp.hr.models import Employee, PayrollLine, PayrollRecord


async def test_deduction_only_payroll_posts_voucher(session, monkeypatch):
    from erp.hr import rate_settlement

    async def _keep(session, record):   # 정산 실행 없이 넣은 소급 공제 줄을 재동기화가 지우지 않게(전표 로직만 본다)
        return None
    monkeypatch.setattr(rate_settlement, "attach_lines", _keep)
    e = Employee(employee_no="DO-1", name="공제만", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                 job_group="STAFF", source="ERP")
    session.add(e)
    await session.flush()
    r = PayrollRecord(employee_id=e.id, yyyymm="2026-10")
    session.add(r)
    await session.flush()
    session.add_all([PayrollLine(record_id=r.id, item_code="INCENTIVE", item_kind="EARNING", amount=1_000_000),
                     PayrollLine(record_id=r.id, item_code="NP_ADJ", item_kind="DEDUCTION", amount=12_000)])
    await session.flush()
    await PayrollService().confirm(session, actor=1, employee_id=e.id, yyyymm="2026-10")
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == f"payroll:{r.id}"))
    assert v is not None
    lines = {(ln.account_code, ln.side): float(ln.amount) for ln in
             (await session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == v.id))).all()}
    assert lines == {("2110", "DEBIT"): 12_000, ("2150", "CREDIT"): 12_000}


async def test_incentive_only_without_deduction_posts_nothing(session):
    e = Employee(employee_no="DO-2", name="성과급만", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                 job_group="STAFF", source="ERP")
    session.add(e)
    await session.flush()
    r = PayrollRecord(employee_id=e.id, yyyymm="2026-10")
    session.add(r)
    await session.flush()
    session.add(PayrollLine(record_id=r.id, item_code="INCENTIVE", item_kind="EARNING", amount=1_000_000))
    await session.flush()
    await PayrollService().confirm(session, actor=1, employee_id=e.id, yyyymm="2026-10")
    assert await session.scalar(select(Voucher).where(Voucher.source_ref == f"payroll:{r.id}")) is None
