"""Clinic 직원 동기화 (FR-28) — SSRF·파싱·hr.employees upsert·매핑·멱등."""
from datetime import date

import httpx
import pytest
from sqlalchemy import func, select

from erp.common.models import Department
from erp.hr.models import Employee, License
from erp.hr.staff_sync import consume_clinic_staff
from erp.integrations.clinic_staff import RestClinicStaffSource
from erp.integrations.sources import RawStaffDTO

STAFF = [
    {"id": "cln-doc-1", "username": "김의사", "email": "doc@example.org",
     "clinicRole": "MEMBER", "hisRole": "Doctor", "department": "내과",
     "licenseNumber": "MD-1001", "joinedAt": "2025-03-02T00:00:00Z"},
    {"id": "cln-nrs-1", "username": "이간호", "email": "nrs@example.org",
     "hisRole": "Nurse", "department": "간호부", "licenseNumber": "RN-2002",
     "joinedAt": "2024-06-01T00:00:00Z"},
    {"username": "결손", "hisRole": "Admin"},  # id 없음 → 스킵
]


def _source(staff=STAFF) -> RestClinicStaffSource:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Hospital-Code"] == "WRB-DEMO-M66M"
        assert request.headers["authorization"] == "Bearer clinic_k"
        assert request.url.path == "/api/clinic/his/staff"
        return httpx.Response(200, json={"staff": staff})

    return RestClinicStaffSource(
        "https://clinic.test", "clinic_k", "WRB-DEMO-M66M",
        allowed_hosts=frozenset({"clinic.test"}), transport=httpx.MockTransport(handler))


def test_ssrf_guard():
    with pytest.raises(ValueError, match="허용되지 않은 그룹웨어"):
        RestClinicStaffSource("https://evil.example", "k", "H")


def test_fetch_parses_and_skips_idless():
    dtos = list(_source().fetch_staff())
    assert len(dtos) == 2
    assert dtos[0].external_user_id == "cln-doc-1" and dtos[0].his_role == "Doctor"
    assert dtos[0].joined_on == date(2025, 3, 2) and dtos[0].license_no == "MD-1001"


class _Stub:
    def __init__(self, dtos):
        self._dtos = dtos

    def fetch_staff(self):
        return iter(self._dtos)


async def test_consume_creates_employees_dept_license(session):
    out = await consume_clinic_staff(session, _source())
    assert out == {"created": 2, "updated": 0}
    emps = (await session.execute(
        select(Employee).order_by(Employee.employee_no))).scalars().all()
    assert [e.employee_no for e in emps] == ["C0001", "C0002"]
    doc = next(e for e in emps if e.external_user_id == "cln-doc-1")
    assert doc.job_group == "DOCTOR" and doc.name == "김의사"
    # 부서 생성/연결
    depts = {d.name for d in (await session.execute(select(Department))).scalars().all()}
    assert {"내과", "간호부"} <= depts
    # 면허 등록
    lic = (await session.execute(select(License).where(License.employee_id == doc.id))).scalar_one()
    assert lic.license_no == "MD-1001" and lic.license_type == "의사"


async def test_consume_idempotent_by_external_id(session):
    await consume_clinic_staff(session, _source())
    out2 = await consume_clinic_staff(session, _source())
    assert out2 == {"created": 0, "updated": 2}  # external_user_id 멱등 — 갱신만
    assert (await session.scalar(select(func.count()).select_from(Employee))) == 2


async def test_consume_job_group_mapping(session):
    dtos = [
        RawStaffDTO("u1", "약사", "p@x", "Pharmacist", "약제팀", "PH-1", None),
        RawStaffDTO("u2", "기사", "t@x", "LabTech", "검사실", None, None),
        RawStaffDTO("u3", "행정", "a@x", "Admin", "행정팀", None, None),
    ]
    await consume_clinic_staff(session, _Stub(dtos))
    by_ext = {e.external_user_id: e for e in
              (await session.execute(select(Employee))).scalars().all()}
    assert by_ext["u1"].job_group == "STAFF"   # Pharmacist → STAFF
    assert by_ext["u2"].job_group == "TECH"    # LabTech → TECH
    assert by_ext["u3"].job_group == "STAFF"   # Admin → STAFF
