"""인증 — 잠금·refresh 원자 CAS 회전·재사용 탐지·token_version (design §6.1)."""
from datetime import datetime, timezone

import pytest

from erp.common.auth_service import AuthService
from erp.common.models import User
from erp.common.security import hash_password, validate_password_policy
from erp.errors import AccountLockedError, AuthFailedError, TokenError

svc = AuthService()  # Redis 없음 — DB 폴백 경로


async def _make_user(session, email="nurse@hospital.example.org", password="Secure!Pass12"):
    user = User(email=email, password_hash=hash_password(password), role="STAFF")
    session.add(user)
    await session.flush()
    return user


async def test_login_success_returns_token_pair(session):
    await _make_user(session)
    tokens = await svc.login(session, email="nurse@hospital.example.org", password="Secure!Pass12")
    assert tokens["access_token"] and tokens["refresh_token"]


async def test_login_wrong_password_fails(session):
    await _make_user(session)
    with pytest.raises(AuthFailedError):
        await svc.login(session, email="nurse@hospital.example.org", password="wrong-password1")


async def test_lockout_after_5_failures(session):
    user = await _make_user(session)
    for _ in range(5):
        with pytest.raises(AuthFailedError):
            await svc.login(session, email="nurse@hospital.example.org", password="wrong-password1")
    assert user.locked_until and user.locked_until > datetime.now(timezone.utc)
    with pytest.raises(AccountLockedError):
        await svc.login(session, email="nurse@hospital.example.org", password="Secure!Pass12")


async def test_refresh_rotation_old_token_rejected_after_grace(session, monkeypatch):
    """회전 후 구 토큰 재사용(grace 밖) → family 폐기 + 전 세션 무효화."""
    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "refresh_grace_seconds", 0)

    user = await _make_user(session)
    t1 = await svc.login(session, email="nurse@hospital.example.org", password="Secure!Pass12")
    t2 = await svc.refresh(session, raw_refresh=t1["refresh_token"])
    assert t2["refresh_token"] != t1["refresh_token"]

    tv_before = user.token_version
    with pytest.raises(TokenError):
        await svc.refresh(session, raw_refresh=t1["refresh_token"])  # 재사용
    await session.refresh(user)
    assert user.token_version == tv_before + 1  # 전 세션 무효화

    # family 전체 폐기 — 신 토큰도 사용 불가
    with pytest.raises(TokenError):
        await svc.refresh(session, raw_refresh=t2["refresh_token"])


async def test_refresh_reuse_within_grace_is_soft_rejected(session):
    """grace 내 재사용(동시 탭 오탐) — 거부하되 family는 유지."""
    user = await _make_user(session)
    t1 = await svc.login(session, email="nurse@hospital.example.org", password="Secure!Pass12")
    t2 = await svc.refresh(session, raw_refresh=t1["refresh_token"])

    tv_before = user.token_version
    with pytest.raises(TokenError):
        await svc.refresh(session, raw_refresh=t1["refresh_token"])  # grace 내
    await session.refresh(user)
    assert user.token_version == tv_before          # 무효화 안 됨
    t3 = await svc.refresh(session, raw_refresh=t2["refresh_token"])  # family 생존
    assert t3["refresh_token"]


async def test_logout_bumps_token_version_and_revokes(session):
    user = await _make_user(session)
    await svc.login(session, email="nurse@hospital.example.org", password="Secure!Pass12")
    tv = user.token_version
    await svc.logout(session, user_id=user.id)
    await session.refresh(user)
    assert user.token_version == tv + 1


def test_password_policy():
    assert validate_password_policy("Secure!Pass12")
    assert not validate_password_policy("short1!")          # 10자 미만
    assert not validate_password_policy("aaaaaaaaaaaa")     # 1종
