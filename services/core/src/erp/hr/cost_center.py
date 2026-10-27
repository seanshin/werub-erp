"""직원 소속 부서 → cost_center 해석 (G9 인건비 부서 귀속).

급여·성과급·퇴직 전기의 비용(DR) 라인에 소속 부서 코드를 cost_center로 스탬프해, 부서별 인건비가
`dimension_summary`(cost_center) 리포트에 잡히게 한다. 자산(asset_service._cost_center_for)과 동일 관례.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def employee_cost_center(session: AsyncSession, employee_id: int,
                               cache: dict | None = None) -> str | None:
    """직원 소속 부서 코드(없으면 DEPT-{id}, 부서 미지정이면 None). 배치는 cache로 조회 최소화."""
    if cache is not None and employee_id in cache:
        return cache[employee_id]
    from erp.common.models import Department
    from erp.hr.models import Employee
    emp = await session.get(Employee, employee_id)
    cc: str | None = None
    if emp is not None and emp.department_id is not None:
        code = await session.scalar(
            select(Department.code).where(Department.id == emp.department_id))
        cc = code or f"DEPT-{emp.department_id}"
    if cache is not None:
        cache[employee_id] = cc
    return cc
