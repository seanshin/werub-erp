"""ERP 내부 권한 배분 — 사용자 역할 변경·활성토글(HIS 접근과 별개)."""
import pytest

from erp.app.admin_router import (
    UserActiveBody, UserRoleBody, change_user_role, list_users, set_user_active,
)
from erp.common.models import User
from erp.common.router import CurrentUser
from erp.errors import ForbiddenError, ValidationError


def _admin(uid: int) -> CurrentUser:
    return CurrentUser(user_id=uid, role="ADMIN", dept_id=None, license_flag=True)


async def _seed(session):
    a = User(email="admin@x.example", password_hash="x", role="ADMIN")
    b = User(email="staff@x.example", password_hash="x", role="STAFF")
    session.add_all([a, b])
    await session.flush()
    return a, b


async def test_admin_changes_role_and_bumps_token_version(session):
    a, b = await _seed(session)
    tv0 = b.token_version
    out = await change_user_role(b.id, UserRoleBody(role="MANAGER"),
                                 user=_admin(a.id), session=session)
    assert out["role"] == "MANAGER" and out["before"] == "STAFF"
    await session.refresh(b)
    assert b.role == "MANAGER" and b.token_version == tv0 + 1   # 강제 재인증


async def test_non_admin_forbidden(session):
    a, b = await _seed(session)
    with pytest.raises(ForbiddenError):
        await change_user_role(b.id, UserRoleBody(role="ADMIN"),
                               user=CurrentUser(user_id=9, role="MANAGER", dept_id=None,
                                                license_flag=True), session=session)


async def test_self_role_change_blocked(session):
    a, _ = await _seed(session)
    with pytest.raises(ValidationError, match="본인"):
        await change_user_role(a.id, UserRoleBody(role="STAFF"),
                               user=_admin(a.id), session=session)


async def test_last_admin_demote_blocked(session):
    a, _ = await _seed(session)   # a = 유일 ADMIN
    with pytest.raises(ValidationError, match="마지막 ADMIN"):
        await change_user_role(a.id, UserRoleBody(role="STAFF"),
                               user=_admin(99999), session=session)


async def test_invalid_role_rejected(session):
    a, b = await _seed(session)
    with pytest.raises(ValidationError, match="허용되지 않은"):
        await change_user_role(b.id, UserRoleBody(role="SUPERUSER"),
                               user=_admin(a.id), session=session)


async def test_domain_manager_roles_accepted(session):
    """도메인 관리자 역할(#5) — IT/회계/물류 관리자 배정 허용."""
    a, b = await _seed(session)
    for role in ("IT_MANAGER", "FINANCE_MANAGER", "LOGISTICS_MANAGER"):
        out = await change_user_role(b.id, UserRoleBody(role=role),
                                     user=_admin(a.id), session=session)
        assert out["role"] == role


async def test_list_users_filter(session):
    a, b = await _seed(session)
    allu = await list_users(user=_admin(a.id), session=session)
    assert len(allu["items"]) >= 2
    staff = await list_users(role="STAFF", user=_admin(a.id), session=session)
    assert staff["items"] and all(u["role"] == "STAFF" for u in staff["items"])


async def test_set_active_and_self_block(session):
    a, b = await _seed(session)
    out = await set_user_active(b.id, UserActiveBody(active=False),
                                user=_admin(a.id), session=session)
    assert out["is_active"] is False
    await session.refresh(b)
    assert b.is_active is False and b.token_version == 1   # 비활성 시 세션 무효
    with pytest.raises(ValidationError, match="본인"):
        await set_user_active(a.id, UserActiveBody(active=False),
                              user=_admin(a.id), session=session)
