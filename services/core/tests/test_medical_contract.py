"""의료진 개별 계약 (P4-1) — 등록·승계·검증."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.hr.contract_service import MedicalContractService
from erp.hr.employee_service import EmployeeService

emp_svc = EmployeeService()
svc = MedicalContractService()


async def _doctor(session, no="D-1"):
    return await emp_svc.create(session, actor=1, employee_no=no, name="의사",
                                hired_on=date(2025, 1, 1), job_group="DOCTOR")


async def test_create_active_contract(session):
    d = await _doctor(session)
    c = await svc.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 1, 1),
                         base_pay=8_000_000, duty_pay=150_000,
                         incentive_type="NET_COLLECTION", incentive_rate=15.0,
                         incentive_params={"cap": 5_000_000})
    assert c.status == "ACTIVE" and float(c.base_pay) == 8_000_000
    assert c.incentive_type == "NET_COLLECTION" and float(c.incentive_rate) == 15.0


async def test_new_contract_supersedes_previous(session):
    d = await _doctor(session, "D-2")
    c1 = await svc.create(session, actor=1, employee_id=d.id, valid_from=date(2025, 1, 1),
                          base_pay=7_000_000)
    c2 = await svc.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 1, 1),
                          base_pay=8_500_000, incentive_type="NET_COLLECTION", incentive_rate=20)
    assert c1.status == "TERMINATED" and c1.valid_to == date(2025, 12, 31)
    assert c2.status == "ACTIVE"
    cur = await svc.active(session, employee_id=d.id)
    assert cur.id == c2.id and float(cur.base_pay) == 8_500_000


async def test_only_medical_staff(session):
    e = await emp_svc.create(session, actor=1, employee_no="A-1", name="행정",
                             hired_on=date(2025, 1, 1), job_group="STAFF")
    with pytest.raises(ValidationError, match="의료진"):
        await svc.create(session, actor=1, employee_id=e.id, valid_from=date(2026, 1, 1),
                         base_pay=4_000_000)


async def test_incentive_requires_rate(session):
    d = await _doctor(session, "D-3")
    with pytest.raises(ValidationError, match="배분율"):
        await svc.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 1, 1),
                         base_pay=8_000_000, incentive_type="NET_COLLECTION")


async def test_new_start_must_be_after_previous(session):
    d = await _doctor(session, "D-4")
    await svc.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 6, 1),
                     base_pay=8_000_000)
    with pytest.raises(ValidationError, match="시작일"):
        await svc.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 1, 1),
                         base_pay=9_000_000)


async def test_approval_flow_with_segregation_and_supersede(session):
    from erp.common.contract_lifecycle import assert_transition  # noqa: F401 — 커널 사용 확인
    d = await _doctor(session, "D-5")
    cur = await svc.create(session, actor=1, employee_id=d.id, valid_from=date(2025, 1, 1),
                           base_pay=7_000_000)                         # ACTIVE
    nxt = await svc.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 1, 1),
                           base_pay=9_000_000, require_approval=True)  # DRAFT(승계 보류)
    assert nxt.status == "DRAFT" and cur.status == "ACTIVE"
    await svc.submit_for_approval(session, actor=1, contract_id=nxt.id)
    with pytest.raises(ValidationError, match="직무분리"):
        await svc.approve(session, actor=1, contract_id=nxt.id)
    await svc.approve(session, actor=2, contract_id=nxt.id)
    assert nxt.status == "APPROVED" and nxt.approved_by == 2
    await svc.activate(session, actor=2, contract_id=nxt.id, on=date(2026, 1, 1))
    assert nxt.status == "ACTIVE"
    assert cur.status == "TERMINATED" and cur.valid_to == date(2025, 12, 31)  # 발효 시 승계


async def test_illegal_transition_blocked(session):
    d = await _doctor(session, "D-6")
    c = await svc.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 1, 1),
                         base_pay=8_000_000)     # ACTIVE
    with pytest.raises(ValidationError, match="전이"):
        await svc.submit_for_approval(session, actor=1, contract_id=c.id)  # ACTIVE→PENDING 불가


async def test_amend_pay_records_history(session):
    from erp.common.models import ContractAmendment
    from sqlalchemy import select
    d = await _doctor(session, "D-7")
    c = await svc.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 1, 1),
                         base_pay=8_000_000)
    await svc.amend_pay(session, actor=1, contract_id=c.id, new_base_pay=9_500_000,
                        effective_on=date(2026, 7, 1), reason="연봉 조정")
    assert float(c.base_pay) == 9_500_000
    am = (await session.execute(select(ContractAmendment).where(
        ContractAmendment.contract_kind == "MEDICAL",
        ContractAmendment.contract_id == c.id))).scalar_one()
    assert am.change_type == "AMOUNT" and am.new_value == "9500000"


async def test_terminate_with_reason(session):
    d = await _doctor(session, "D-8")
    c = await svc.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 1, 1),
                         base_pay=8_000_000)
    await svc.terminate(session, actor=1, contract_id=c.id, end_on=date(2026, 6, 30),
                        reason="퇴직")
    assert c.status == "TERMINATED" and c.terminate_reason == "퇴직"
    assert c.valid_to == date(2026, 6, 30) and c.terminated_on == date(2026, 6, 30)
