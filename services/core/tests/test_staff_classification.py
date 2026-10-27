"""직군·직종·고용형태 + 인사 SoT(source) 가드 (의료기관 분리, D-2)."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ForbiddenError
from erp.hr.employee_service import EmployeeService
from erp.hr.models import Employee
from erp.integrations.inbound_consumer import InboundConsumer
from erp.integrations.models import HisInboundEvent

svc = EmployeeService()


async def test_erp_created_is_source_erp_with_classification(session):
    emp = await svc.create(session, actor=1, employee_no="E-DAILY-1", name="일용김",
                           hired_on=date(2026, 1, 1), job_group="STAFF",
                           employment_type="DAILY", daily_wage=150000)
    assert emp.source == "ERP"                 # ERP 생성 = ERP 관리
    assert emp.staff_class == "SUPPORT" and emp.employment_type == "DAILY"
    assert float(emp.daily_wage) == 150000


async def test_medical_class_derived_from_job_group(session):
    emp = await svc.create(session, actor=1, employee_no="E-NUR-1", name="간호박",
                           hired_on=date(2026, 1, 1), job_group="NURSE",
                           employment_type="CONTRACT")
    assert emp.staff_class == "MEDICAL" and emp.job_category == "NURSE"


async def test_transfer_blocked_for_his_managed(session):
    emp = await svc.create(session, actor=1, employee_no="E-HIS-1", name="의사",
                           hired_on=date(2026, 1, 1), job_group="DOCTOR")
    emp.source = "HIS"                          # HIS 관리로 전환(미러 가정)
    await session.flush()
    with pytest.raises(ForbiddenError, match="HIS 관리"):
        await svc.transfer(session, actor=1, employee_id=emp.id,
                           to_department_id=2, to_position="과장", effective_on=date(2026, 6, 1))


async def test_consumer_does_not_overwrite_erp_managed(session):
    # ERP 관리 직원과 동일 사번을 HIS가 보내도 덮어쓰지 않음 (SoT 가드)
    erp = await svc.create(session, actor=1, employee_no="DR-050", name="ERP원본",
                           hired_on=date(2026, 1, 1), job_group="STAFF")
    assert erp.source == "ERP"
    session.add(HisInboundEvent(domain="hr", event_type="staff.changed", source_ref="EVT-G1",
                                payload={"employeeNumber": "DR-050", "name": "HIS덮어쓰기"},
                                status="NEW"))
    await session.flush()
    out = await InboundConsumer().consume(session)
    assert out["applied"] == 1                  # skip도 성공 처리(APPLIED)
    e = await session.scalar(select(Employee).where(Employee.employee_no == "DR-050"))
    assert e.name == "ERP원본" and e.source == "ERP"   # 변경 안 됨


async def test_consumer_created_staff_is_source_his(session):
    session.add(HisInboundEvent(domain="hr", event_type="staff.created", source_ref="EVT-G2",
                                payload={"employeeNumber": "DR-060", "name": "정규의",
                                         "status": "ACTIVE"}, status="NEW"))
    await session.flush()
    await InboundConsumer().consume(session)
    e = await session.scalar(select(Employee).where(Employee.employee_no == "DR-060"))
    assert e.source == "HIS" and e.employment_type == "REGULAR"
