"""Clinic 직원 동기화 소비 (FR-28) — Clinic 직원 → hr.employees upsert.

설계 §5.2: 코어 쓰기는 소비 단계에서만. external_user_id(Clinic id) 기준 멱등 upsert.
부서는 이름으로 해소/생성, 면허는 license_no 기준 추가. hisRole → job_group 매핑.
"""
import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.models import Department
from erp.hr.models import Employee, License

logger = logging.getLogger("erp.hr.staff_sync")


def _job_group(his_role: str | None) -> str:
    """hisRole → ERP job_group (노동규제 룰 분기용). 미상은 STAFF."""
    r = (his_role or "").lower()
    return {"doctor": "DOCTOR", "nurse": "NURSE", "resident": "RESIDENT",
            "labtech": "TECH", "radiologist": "TECH"}.get(r, "STAFF")


def _license_type(his_role: str | None) -> str:
    r = (his_role or "").lower()
    return {"doctor": "의사", "nurse": "간호사", "pharmacist": "약사",
            "labtech": "임상병리사"}.get(r, "기타")


async def _next_employee_no(session: AsyncSession) -> str:
    """Clinic 동기화 직원 사번 — C 접두 + 4자리 (내부 E 사번과 분리)."""
    last = await session.scalar(
        select(func.max(Employee.employee_no)).where(Employee.employee_no.like("C%")))
    n = (int(last[1:]) + 1) if (last and last[1:].isdigit()) else 1
    return f"C{n:04d}"


async def consume_clinic_staff(session: AsyncSession, source) -> dict:
    """Clinic 직원 수신 → hr.employees upsert(external_user_id 멱등). 반환 {created, updated}."""
    dept_by_name = {
        name: did for name, did in
        (await session.execute(select(Department.name, Department.id))).all()}
    created = updated = 0

    for dto in source.fetch_staff():
        dept_id = None
        if dto.department:
            dept_id = dept_by_name.get(dto.department)
            if dept_id is None:
                d = Department(name=dto.department)
                session.add(d)
                await session.flush()
                dept_id = d.id
                dept_by_name[dto.department] = dept_id

        jg = _job_group(dto.his_role)
        emp = await session.scalar(
            select(Employee).where(Employee.external_user_id == dto.external_user_id))
        if emp is None:
            emp = Employee(
                employee_no=await _next_employee_no(session),
                external_user_id=dto.external_user_id, name=dto.name,
                department_id=dept_id, job_group=jg, position=dto.his_role,
                hired_on=dto.joined_on or date.today())
            session.add(emp)
            await session.flush()
            created += 1
        else:
            emp.name = dto.name
            emp.department_id = dept_id
            emp.job_group = jg
            emp.position = dto.his_role
            updated += 1

        if dto.license_no:
            exists = await session.scalar(select(License.id).where(
                License.employee_id == emp.id, License.license_no == dto.license_no))
            if exists is None:
                session.add(License(employee_id=emp.id,
                                    license_type=_license_type(dto.his_role),
                                    license_no=dto.license_no))

    logger.info("Clinic 직원 동기화", extra={"ctx": {"created": created, "updated": updated}})
    return {"created": created, "updated": updated}
