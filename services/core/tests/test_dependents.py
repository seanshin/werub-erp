"""부양가족 마스터 (P3) — 자동 제안·공제 인원·급여 폴백 체인."""
from datetime import date

from erp.hr.compensation_service import CompensationService
from erp.hr.dependent_service import DependentService, suggest_counted
from erp.hr.models import Employee
from erp.hr.router import _resolve_pay_basis

svc = DependentService()


def test_suggest_counted_age_rules():
    on = date(2026, 6, 12)
    assert suggest_counted("SPOUSE", None, False, on) is True
    assert suggest_counted("CHILD", date(2008, 1, 1), False, on) is True     # 18세
    assert suggest_counted("CHILD", date(2004, 1, 1), False, on) is False    # 22세
    assert suggest_counted("PARENT", date(1960, 1, 1), False, on) is True    # 66세
    assert suggest_counted("PARENT", date(1975, 1, 1), False, on) is False   # 51세
    assert suggest_counted("SIBLING", date(2010, 1, 1), True, on) is True    # 장애인
    assert suggest_counted("CHILD", None, False, on) is False                # 생년 미상


async def test_payroll_dependents_and_fallback_chain(session):
    emp = Employee(employee_no="E0600", name="가족왕", hired_on=date(2025, 1, 1),
                   job_group="STAFF", employment_type="REGULAR", source="ERP")
    session.add(emp)
    await session.flush()
    await CompensationService().upsert(session, actor=1, employee_id=emp.id,
                                       base_pay=3_000_000, dependents=1)

    # 가족 미등록 → 프로필 폴백(1)
    base, dep = await _resolve_pay_basis(session, emp, None, None)
    assert dep == 1

    await svc.add(session, actor=1, employee_id=emp.id, name="배우자",
                  relation="SPOUSE")                                  # counted 자동 True
    await svc.add(session, actor=1, employee_id=emp.id, name="자녀",
                  relation="CHILD", birth_date=date(2015, 5, 5))      # True (11세)
    d3 = await svc.add(session, actor=1, employee_id=emp.id, name="부친",
                       relation="PARENT", birth_date=date(1972, 1, 1))  # False (54세)
    assert d3.counted is False

    assert await svc.payroll_dependents(session, emp.id) == 3         # 본인+배우자+자녀
    base, dep = await _resolve_pay_basis(session, emp, None, None)
    assert dep == 3                                                   # 가족 마스터 우선
    _, dep2 = await _resolve_pay_basis(session, emp, None, 5)
    assert dep2 == 5                                                  # 명시 입력 최우선

    await svc.remove(session, actor=1, dependent_id=d3.id)
    assert len(await svc.list(session, employee_id=emp.id)) == 2
