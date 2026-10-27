"""법정의무교육·건강검진 (P3) — 기록 멱등·만료 산정·매트릭스·특수검진 대상."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.hr.compliance_service import ComplianceService
from erp.hr.models import Employee

svc = ComplianceService()


def _emp(no, name, job_category=None, resigned=None):
    return Employee(employee_no=no, name=name, hired_on=date(2025, 1, 1),
                    job_group="STAFF", job_category=job_category,
                    employment_type="REGULAR", source="ERP", resigned_on=resigned)


async def test_record_idempotent_and_validity(session):
    emp = _emp("E0500", "김교육")
    session.add(emp)
    await session.flush()

    rec = await svc.record(session, actor=1, employee_id=emp.id,
                           category="PRIVACY", done_on=date(2026, 3, 1))
    assert rec.valid_until == date(2027, 3, 1)             # 연 1회 주기
    dup = await svc.record(session, actor=1, employee_id=emp.id,
                           category="PRIVACY", done_on=date(2026, 3, 1))
    assert dup.id == rec.id                                # 멱등
    with pytest.raises(ValidationError, match="알 수 없는 항목"):
        await svc.record(session, actor=1, employee_id=emp.id,
                         category="NOPE", done_on=date(2026, 3, 1))


async def test_status_matrix_states(session):
    staff = _emp("E0510", "박행정")
    rad = _emp("E0511", "방사선", job_category="RADIOTECH")
    gone = _emp("E0512", "퇴직자", resigned=date(2026, 1, 31))
    session.add_all([staff, rad, gone])
    await session.flush()

    await svc.record(session, actor=1, employee_id=staff.id,        # OK (만료 멀음)
                     category="PRIVACY", done_on=date(2026, 5, 1))
    await svc.record(session, actor=1, employee_id=staff.id,        # OVERDUE
                     category="SAFETY", done_on=date(2025, 1, 10))
    await svc.record(session, actor=1, employee_id=staff.id,        # DUE_SOON (만료 20일 전)
                     category="SEXUAL_HARASSMENT", done_on=date(2025, 7, 1))
    await svc.record(session, actor=1, employee_id=rad.id,          # 특수검진 OK
                     category="CHECKUP_SPECIAL", done_on=date(2026, 2, 1))

    out = await svc.status(session, as_of=date(2026, 6, 11))
    by = {i["employee_no"]: i["cells"] for i in out["items"]}
    assert "E0512" not in by                               # 퇴직자 제외
    assert by["E0510"]["PRIVACY"]["state"] == "OK"
    assert by["E0510"]["SAFETY"]["state"] == "OVERDUE"
    assert by["E0510"]["SEXUAL_HARASSMENT"]["state"] == "DUE_SOON"
    assert by["E0510"]["DISABILITY"]["state"] == "NONE"
    assert by["E0510"]["CHECKUP_SPECIAL"]["state"] == "N/A"   # 방사선 아님 → 비대상
    assert by["E0511"]["CHECKUP_SPECIAL"]["state"] == "OK"

    summ = {s["category"]: s for s in out["summary"]}
    assert summ["CHECKUP_SPECIAL"]["required"] == 1           # 방사선 1명만 대상
    assert summ["PRIVACY"]["required"] == 2 and summ["PRIVACY"]["compliant"] == 1
    assert summ["PRIVACY"]["rate"] == 0.5
