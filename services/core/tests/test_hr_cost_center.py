"""G9 — HR 인건비 부서 cost_center 귀속(급여 GL). 부서별 인건비 dimension 리포트 근거."""
from datetime import date

from sqlalchemy import select

from erp.common.models import Department
from erp.finance.models import Account, Voucher, VoucherLine
from erp.hr.cost_center import employee_cost_center
from erp.hr.employee_service import PayrollService
from erp.hr.models import Employee, PayrollLine, PayrollRecord

svc = PayrollService()


async def _accounts(session):
    for code, name, kind in [("5310", "급여비용", "EXPENSE"), ("2150", "예수금", "LIABILITY"),
                             ("2110", "미지급금", "LIABILITY")]:
        session.add(Account(code=code, name=name, kind=kind))
    await session.flush()


async def test_employee_cost_center_resolves_dept_code(session):
    session.add(Department(id=7, name="내과", code="IM"))
    e = Employee(employee_no="E1", name="김의사", hired_on=date(2025, 1, 1), department_id=7)
    session.add(e)
    await session.flush()
    assert await employee_cost_center(session, e.id) == "IM"
    # 부서 미지정 → None
    e2 = Employee(employee_no="E2", name="무소속", hired_on=date(2025, 1, 1))
    session.add(e2)
    await session.flush()
    assert await employee_cost_center(session, e2.id) is None


async def test_payroll_gl_stamps_department_cost_center(session):
    await _accounts(session)
    session.add(Department(id=7, name="내과", code="IM"))
    e = Employee(employee_no="E1", name="김의사", hired_on=date(2025, 1, 1), department_id=7)
    session.add(e)
    await session.flush()
    rec = PayrollRecord(employee_id=e.id, yyyymm="2026-06")
    session.add(rec)
    await session.flush()
    session.add_all([
        PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=3_000_000),
        PayrollLine(record_id=rec.id, item_code="TAX", item_kind="DEDUCTION", amount=200_000),
    ])
    await session.flush()

    await svc._post_payroll_gl(session, rec, actor=1)

    v = await session.scalar(select(Voucher).where(Voucher.source_ref == f"payroll:{rec.id}"))
    exp = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.account_code == "5310"))
    assert exp.cost_center == "IM"                        # 급여비용 = 소속 부서 귀속
    # 부채 라인(예수금·미지급)은 cost_center 미부여(부서 귀속 아님)
    liab = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.account_code == "2110"))
    assert liab.cost_center is None
