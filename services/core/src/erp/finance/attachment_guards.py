"""finance 첨부 행 수준 가드 — 경비 청구 영수증 소유권/부서 경계(IDOR 차단·hr 가드 동형).

common.attachment_router는 도메인을 import할 수 없으므로 finance가 자신의 가드를 등록한다
(의존성 역전). finance 패키지 import 시 register()가 실행된다.
"""
from sqlalchemy import select

from erp.common.policy import ScopeKind
from erp.errors import ForbiddenError


async def _self_employee_id(session, user) -> int | None:
    from erp.common.models import User
    return (await session.execute(
        select(User.employee_id).where(User.id == user.user_id))).scalar_one_or_none()


async def guard_expense(session, user, scope, entity_id):
    """경비 영수증 — SELF는 본인 청구, DEPT는 본부서 청구만."""
    from erp.finance.models import ExpenseClaim
    claim = await session.get(ExpenseClaim, entity_id)
    if claim is None:
        raise ForbiddenError()
    if scope.kind == ScopeKind.SELF:
        if claim.employee_id != await _self_employee_id(session, user):
            raise ForbiddenError("본인 경비 청구의 영수증만 접근할 수 있습니다")
    elif scope.kind == ScopeKind.DEPT:
        if claim.department_id != user.dept_id:
            raise ForbiddenError("본인 부서 청구의 영수증만 접근할 수 있습니다")


async def exists_expense(session, entity_id) -> bool:
    """유령 청구에 고아 첨부 생성 방지(S-9 동형) — bool 반환 규약."""
    from erp.finance.models import ExpenseClaim
    return await session.get(ExpenseClaim, entity_id) is not None


def register() -> None:
    from erp.common.attachment_router import register_existence_check, register_row_guard
    register_row_guard("expense", guard_expense)
    register_existence_check("expense", exists_expense)
