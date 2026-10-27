"""HR 첨부 행 수준 가드 — 직원·의료진 계약 서류의 소유권/부서 경계 강제(IDOR 차단).

common.attachment_router는 모듈경계상 도메인을 import할 수 없으므로, HR가 자신의
가드를 등록한다(의존성 역전). hr 패키지 import 시 register()가 실행된다.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.policy import ScopeKind
from erp.errors import ForbiddenError


async def _self_employee_id(session: AsyncSession, user) -> int | None:
    from erp.common.models import User
    return (await session.execute(
        select(User.employee_id).where(User.id == user.user_id))).scalar_one_or_none()


async def guard_employee(session, user, scope, entity_id):
    """직원 서류 — SELF는 본인, DEPT는 본인부서 직원 서류만."""
    from erp.hr.models import Employee
    if scope.kind == ScopeKind.SELF:
        if await _self_employee_id(session, user) != entity_id:
            raise ForbiddenError("본인 서류만 접근할 수 있습니다")
    elif scope.kind == ScopeKind.DEPT:
        emp = await session.get(Employee, entity_id)
        if emp is None or emp.department_id != user.dept_id:
            raise ForbiddenError("본인 부서 직원 서류만 접근할 수 있습니다")


async def guard_contract(session, user, scope, entity_id):
    """의료진 계약 서류(보수 민감) — SELF는 본인 계약만. DEPT 정책 없음(도달 시 차단)."""
    from erp.hr.models import MedicalStaffContract
    if scope.kind == ScopeKind.SELF:
        c = await session.get(MedicalStaffContract, entity_id)
        if c is None or c.employee_id != await _self_employee_id(session, user):
            raise ForbiddenError("본인 계약 서류만 접근할 수 있습니다")
    elif scope.kind == ScopeKind.DEPT:
        raise ForbiddenError()


def register() -> None:
    from erp.common.attachment_router import register_row_guard
    register_row_guard("employee", guard_employee)
    register_row_guard("contract", guard_contract)
