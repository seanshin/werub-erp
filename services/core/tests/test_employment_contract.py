"""일반직 근로계약 (근기법 §17·P3-5) — 생성·발효 승계·종료·갱신·의료진 거부·계약직 만기."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.hr.employment_contract_service import EmploymentContractService
from erp.hr.models import Employee

svc = EmploymentContractService()


async def _emp(session, no, staff_class="SUPPORT"):
    e = Employee(employee_no=no, name=f"직원{no}", hired_on=date(2025, 1, 1), job_group="STAFF",
                 employment_type="REGULAR", source="ERP", staff_class=staff_class)
    session.add(e)
    await session.flush()
    return e


async def test_create_draft_then_activate(session):
    e = await _emp(session, "S1")
    c = await svc.create(session, actor=1, employee_id=e.id, contract_type="REGULAR",
                         valid_from=date(2026, 1, 1), base_pay=3000000,
                         weekly_hours=40, work_schedule="월~금 09-18", work_location="본원",
                         job_duties="원무")
    assert c.status == "DRAFT"
    c2 = await svc.activate(session, actor=1, contract_id=c.id)
    assert c2.status == "ACTIVE"


async def test_activate_supersedes_prior(session):
    e = await _emp(session, "S2")
    c1 = await svc.create(session, actor=1, employee_id=e.id, contract_type="REGULAR",
                          valid_from=date(2026, 1, 1), base_pay=3000000, activate=True)
    assert c1.status == "ACTIVE"
    # 신규 발효 → 기존 ACTIVE 승계(ENDED·전일 종료)
    c2 = await svc.create(session, actor=1, employee_id=e.id, contract_type="REGULAR",
                          valid_from=date(2026, 7, 1), base_pay=3200000, activate=True)
    rows = await svc.list_by_employee(session, employee_id=e.id)
    by_id = {r["id"]: r for r in rows}
    assert by_id[c1.id]["status"] == "ENDED" and by_id[c1.id]["valid_to"] == "2026-06-30"
    assert by_id[c2.id]["status"] == "ACTIVE"


async def test_medical_rejected(session):
    e = await _emp(session, "M1", staff_class="MEDICAL")
    with pytest.raises(ValidationError):
        await svc.create(session, actor=1, employee_id=e.id, contract_type="REGULAR",
                         valid_from=date(2026, 1, 1), base_pay=5000000)


async def test_fixed_term_requires_valid_to(session):
    e = await _emp(session, "S3")
    with pytest.raises(ValidationError):
        await svc.create(session, actor=1, employee_id=e.id, contract_type="FIXED_TERM",
                         valid_from=date(2026, 1, 1), base_pay=2800000)   # valid_to 없음


async def test_renew_and_expiring(session):
    e = await _emp(session, "S4")
    c = await svc.create(session, actor=1, employee_id=e.id, contract_type="FIXED_TERM",
                         valid_from=date(2026, 1, 1), valid_to=date(2026, 7, 31),
                         base_pay=2800000, activate=True)
    # 만기 임박 목록(as_of 7/10 기준·within 30일 → 7/31 포함)
    exp = await svc.list_expiring(session, within_days=30, as_of=date(2026, 7, 10))
    assert any(x["id"] == c.id and x["d_day"] == 21 for x in exp)
    # 갱신 — 만료일 연장
    c2 = await svc.renew(session, actor=1, contract_id=c.id, new_valid_to=date(2026, 12, 31))
    assert c2.valid_to == date(2026, 12, 31)
    with pytest.raises(ValidationError):   # 과거로 갱신 불가
        await svc.renew(session, actor=1, contract_id=c.id, new_valid_to=date(2026, 6, 1))


async def test_end_contract(session):
    e = await _emp(session, "S5")
    c = await svc.create(session, actor=1, employee_id=e.id, contract_type="REGULAR",
                         valid_from=date(2026, 1, 1), base_pay=3000000, activate=True)
    c2 = await svc.end(session, actor=1, contract_id=c.id, end_on=date(2026, 8, 31))
    assert c2.status == "ENDED" and c2.ended_on == date(2026, 8, 31)
