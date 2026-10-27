"""첫 관리자 부트스트랩(break-glass) — HIS 없이도 ERP 관리자를 만들 수 있다."""
import pytest
from sqlalchemy import select

from erp.bootstrap_admin import apply
from erp.common.auth_service import AuthService
from erp.common.models import User


async def test_creates_admin_who_can_log_in(session):
    action, pw = await apply(session, " First.Admin@Example.org ")
    assert action == "created" and len(pw) >= 20
    u = (await session.execute(select(User).where(User.email == "first.admin@example.org"))
         ).scalar_one()
    assert u.role == "ADMIN" and pw not in (u.password_hash or "")      # 해시만 저장
    tokens = await AuthService().login(session, email="first.admin@example.org", password=pw)
    assert tokens["access_token"]


async def test_promotes_existing_user_and_revokes_sessions(session):
    session.add(User(email="staff@example.org", role="STAFF", password_hash="x",
                     is_active=False, token_version=3))
    await session.flush()
    action, _ = await apply(session, "staff@example.org")
    u = (await session.execute(select(User).where(User.email == "staff@example.org"))).scalar_one()
    assert action == "promoted" and u.role == "ADMIN" and u.is_active and u.token_version == 4


async def test_rejects_non_email(session):
    with pytest.raises(ValueError):
        await apply(session, "admin")
