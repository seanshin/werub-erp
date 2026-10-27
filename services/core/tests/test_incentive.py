"""성과급 산정엔진 (P4-3) — 산식·cap/floor + 계약×실적 결합."""
from datetime import date

from erp.hr.contract_service import MedicalContractService
from erp.hr.employee_service import EmployeeService
from erp.hr.incentive_engine import calculate
from erp.hr.incentive_service import IncentiveService
from erp.patient.models import Patient, Payment, Reception

emp_svc = EmployeeService()
contracts = MedicalContractService()
svc = IncentiveService()


def test_net_collection():
    r = calculate(incentive_type="NET_COLLECTION", incentive_rate=18, params={},
                  net_revenue=10_000_000)
    assert r["amount"] == 1_800_000


def test_grade_factor_applied_before_cap():
    """P1-1 — 등급 배율은 raw에 적용·cap 판정 포함. 미지정 등급=1.0(무영향)."""
    base = calculate(incentive_type="NET_COLLECTION", incentive_rate=10, params={},
                     net_revenue=10_000_000)
    assert base["amount"] == 1_000_000 and base["grade_factor"] == 1.0
    s = calculate(incentive_type="NET_COLLECTION", incentive_rate=10, params={},
                  net_revenue=10_000_000, eval_grade="S")
    assert s["grade_factor"] == 1.2 and s["amount"] == 1_200_000 and s["eval_grade"] == "S"
    d = calculate(incentive_type="NET_COLLECTION", incentive_rate=10, params={},
                  net_revenue=10_000_000, eval_grade="D")
    assert d["amount"] == 800_000
    # 등급 반영 후 cap 적용
    capped = calculate(incentive_type="NET_COLLECTION", incentive_rate=10,
                       params={"cap": 1_100_000}, net_revenue=10_000_000, eval_grade="S")
    assert capped["amount"] == 1_100_000 and capped["capped"] is True


def test_target_excess():
    r = calculate(incentive_type="TARGET_EXCESS", incentive_rate=30,
                  params={"target": 8_000_000}, net_revenue=10_000_000)
    assert r["amount"] == 600_000          # (10M−8M)×30%
    r0 = calculate(incentive_type="TARGET_EXCESS", incentive_rate=30,
                   params={"target": 12_000_000}, net_revenue=10_000_000)
    assert r0["amount"] == 0               # 목표 미달


def test_cap_and_floor():
    capped = calculate(incentive_type="NET_COLLECTION", incentive_rate=50,
                       params={"cap": 3_000_000}, net_revenue=10_000_000)
    assert capped["amount"] == 3_000_000 and capped["capped"] is True
    floored = calculate(incentive_type="NET_COLLECTION", incentive_rate=1,
                        params={"floor": 500_000}, net_revenue=10_000_000)
    assert floored["amount"] == 500_000 and floored["floored"] is True


async def test_calculate_combines_contract_and_revenue(session):
    d = await emp_svc.create(session, actor=1, employee_no="DR-INC", name="의사",
                             hired_on=date(2025, 1, 1), job_group="DOCTOR")
    await contracts.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 1, 1),
                           base_pay=9_000_000, incentive_type="NET_COLLECTION", incentive_rate=20)
    # 실적: 수납 by doctor_id == employee.id
    pt = Patient(chart_no="INC1", name="환자", birth_date=date(1990, 1, 1))
    session.add(pt)
    await session.flush()
    rc = Reception(patient_id=pt.id, dept_code="IM", doctor_id=d.id,
                   received_on=date(2026, 6, 5), source="HIS")
    session.add(rc)
    await session.flush()
    session.add(Payment(reception_id=rc.id, amount=2_000_000, method="CARD",
                        paid_on=date(2026, 6, 5), status="PAID"))
    await session.flush()
    out = await svc.calculate(session, employee_id=d.id, period="2026-06")
    assert out["contract_id"] is not None
    assert out["net_revenue"] == 2_000_000 and out["amount"] == 400_000   # 2M × 20%


async def test_grade_link_optin_applies_confirmed_grade(session):
    """P1-1 — 설정 OFF=무영향, ON+확정 인사평가 등급 시 성과급에 배율 반영."""
    from erp.hr.evaluation_service import EvaluationService
    from erp.hr.payroll_settings import PayrollSettingsService
    d = await emp_svc.create(session, actor=1, employee_no="DR-GL", name="의사",
                             hired_on=date(2025, 1, 1), job_group="DOCTOR")
    await contracts.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 1, 1),
                           base_pay=9_000_000, incentive_type="NET_COLLECTION", incentive_rate=20)
    pt = Patient(chart_no="GL1", name="환자", birth_date=date(1990, 1, 1))
    session.add(pt)
    await session.flush()
    rc = Reception(patient_id=pt.id, dept_code="IM", doctor_id=d.id,
                   received_on=date(2026, 6, 5), source="HIS")
    session.add(rc)
    await session.flush()
    session.add(Payment(reception_id=rc.id, amount=2_000_000, method="CARD",
                        paid_on=date(2026, 6, 5), status="PAID"))
    await session.flush()
    # 확정 인사평가(S등급) — 2026 차수
    es = EvaluationService()
    cyc = await es.create_cycle(session, actor=1, period="2026", title="연간")
    cr = await es.add_criterion(session, actor=1, cycle_id=cyc.id, title="역량")
    await es.open_cycle(session, actor=1, cycle_id=cyc.id)
    ev = await es.assign(session, actor=1, cycle_id=cyc.id, employee_id=d.id, evaluator_id=1)
    await es.submit_self(session, eval_id=ev.id, self_emp=d.id,
                         scores=[{"criterion_id": cr.id, "score": 95}])
    from erp.common.policy import Role, require_scope
    from erp.common.router import CurrentUser
    sc = require_scope(CurrentUser(user_id=1, role=Role.ADMIN, dept_id=None, license_flag=True),
                       "hr.evaluation", "read")
    await es.submit_eval(session, eval_id=ev.id, actor=1, actor_scope=sc,
                         scores=[{"criterion_id": cr.id, "score": 95}])
    await es.confirm(session, eval_id=ev.id, actor=2)   # S(≥90)
    # 설정 OFF(기본) → 배율 없음
    out_off = await svc.calculate(session, employee_id=d.id, period="2026-06")
    assert out_off["amount"] == 400_000 and out_off["eval_grade"] is None
    # 설정 ON → S배율 1.2
    await PayrollSettingsService().upsert(session, actor=1, incentive_withholding_method="NONE",
                                          incentive_grade_link=True)
    out_on = await svc.calculate(session, employee_id=d.id, period="2026-06")
    assert out_on["eval_grade"] == "S" and out_on["amount"] == 480_000   # 400k × 1.2


async def test_no_contract_zero(session):
    d = await emp_svc.create(session, actor=1, employee_no="DR-NOINC", name="의사",
                             hired_on=date(2025, 1, 1), job_group="DOCTOR")
    out = await svc.calculate(session, employee_id=d.id, period="2026-06")
    assert out["amount"] == 0 and out["contract"] is None
