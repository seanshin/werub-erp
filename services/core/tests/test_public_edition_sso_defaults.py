"""공개판 — HIS SSO 정책을 관리자가 정하지 않았을 때의 기본값.

정하지 않으면 URL · 본문으로 HIS 메인 토큰을 넘기는 방식은 막고, erp-sso aud 와 jti 를 요구한다.
관리자가 명시적으로 허용 · 해제하면 그 결정을 따른다.
"""
import uuid

import pytest

import erp.common.org_service as org
from erp.common.models import OrgSettings
from erp.common.org_service import set_sso_policy, sso_policy
from erp.common.sso_service import HisSsoService, SsoTokenModeBlockedError
from erp.errors import AuthFailedError
from tests.test_sso import HIS_SECRET, _his_token


@pytest.fixture
def safe(monkeypatch):
    monkeypatch.setattr(org, "SAFE_DEFAULTS", True)
    from erp.config import get_settings
    s = get_settings()
    old, s.his_jwt_secret = s.his_jwt_secret, HIS_SECRET
    yield
    s.his_jwt_secret = old


async def _org(session):
    session.add(OrgSettings(id=1, name="테스트병원"))
    await session.flush()


async def test_undecided_is_safe(session, safe):
    await _org(session)
    pol = await sso_policy(session)
    assert pol["token_allowed"] is False and pol["audience"] == "erp-sso" and pol["require_jti"] is True
    assert set(pol["undecided"]) == {"token_mode", "enforce_audience"}


async def test_undecided_blocks_url_token(session, safe):
    await _org(session)
    with pytest.raises(SsoTokenModeBlockedError):
        await HisSsoService().login_with_his_token(session, his_token=_his_token("u@his.example", "STAFF"))


async def test_undecided_rejects_main_token_on_cookie_path(session, safe):
    """aud · jti 가 없는 HIS 메인 토큰은 쿠키 경로로도 받지 않는다."""
    await _org(session)
    with pytest.raises(AuthFailedError):
        await HisSsoService().login_with_his_token(
            session, his_token=_his_token("u@his.example", "STAFF"), mode="cookie")


async def test_undecided_accepts_erp_sso_token_on_cookie_path(session, safe):
    await _org(session)
    tok = _his_token("u@his.example", "STAFF", aud="erp-sso", jti=str(uuid.uuid4()))
    out = await HisSsoService().login_with_his_token(session, his_token=tok, mode="cookie")
    assert out["access_token"]


async def test_explicit_admin_decision_restores_legacy(session, safe):
    await _org(session)
    await set_sso_policy(session, token_mode="allow", enforce_audience=False, actor=1)
    pol = await sso_policy(session)
    assert pol["token_allowed"] is True and pol["audience"] is None
    out = await HisSsoService().login_with_his_token(session, his_token=_his_token("l@his.example", "STAFF"))
    assert out["access_token"]
