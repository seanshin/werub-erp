"""Metabase 자동 로그인 브리지 — 세션 발급·쿠키 주입·미설정 가드."""
import pytest

from erp.common.router import CurrentUser
from erp.config import get_settings
from erp.errors import ValidationError
from erp.integrations import bi_sso

_USER = CurrentUser(user_id=1, role="MANAGER", dept_id=None, license_flag=False)


def _mock_client(captured):
    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"id": "mb-session-xyz"}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json):
            captured.update(url=url, json=json)
            return _Resp()
    return _Client


async def test_metabase_login_issues_session_cookie(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "metabase_sso_user", "erp-bi@hospital.example.org", raising=False)
    monkeypatch.setattr(s, "metabase_sso_password", "secret", raising=False)
    monkeypatch.setattr(s, "metabase_internal_base", "http://erp-metabase:3000", raising=False)
    captured = {}
    monkeypatch.setattr(bi_sso.httpx, "AsyncClient", _mock_client(captured))

    resp = await bi_sso.metabase_login(user=_USER)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/erp/mb/"
    # 공용계정으로 세션 요청
    assert captured["url"].endswith("/api/session")
    assert captured["json"]["username"] == "erp-bi@hospital.example.org"
    # metabase.SESSION 쿠키 주입(httpOnly·path /erp/mb)
    sc = resp.headers["set-cookie"]
    assert "metabase.SESSION=mb-session-xyz" in sc
    assert "Path=/erp/mb" in sc and "HttpOnly" in sc


async def test_metabase_login_disabled_without_config(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "metabase_sso_user", None, raising=False)
    monkeypatch.setattr(s, "metabase_sso_password", None, raising=False)
    with pytest.raises(ValidationError, match="미설정"):
        await bi_sso.metabase_login(user=_USER)


async def test_metabase_login_forbidden_for_non_bi_role(monkeypatch):
    """공용 세션 발급 전 인가 게이트 — 경영 외 역할(WORKSTATION 등)은 403(전체검토 2026-07-03)."""
    from erp.errors import ForbiddenError
    s = get_settings()
    monkeypatch.setattr(s, "metabase_sso_user", "erp-bi@hospital.example.org", raising=False)
    monkeypatch.setattr(s, "metabase_sso_password", "secret", raising=False)
    # HTTP 자체가 호출되면 안 됨(게이트에서 먼저 차단) — 호출 시 실패하는 클라이언트
    class _Boom:
        def __init__(self, *a, **k): raise AssertionError("역할 게이트 전에 세션 발급 시도")
    monkeypatch.setattr(bi_sso.httpx, "AsyncClient", _Boom)

    for role in ("WORKSTATION", "STAFF", "VIEWER", "NURSE"):
        u = CurrentUser(user_id=9, role=role, dept_id=None, license_flag=False)
        with pytest.raises(ForbiddenError):
            await bi_sso.metabase_login(user=u)


async def test_metabase_login_allows_bi_roles(monkeypatch):
    """경영 역할(ADMIN·MANAGER·DIRECTOR)은 통과."""
    s = get_settings()
    monkeypatch.setattr(s, "metabase_sso_user", "erp-bi@hospital.example.org", raising=False)
    monkeypatch.setattr(s, "metabase_sso_password", "secret", raising=False)
    monkeypatch.setattr(s, "metabase_internal_base", "http://erp-metabase:3000", raising=False)
    monkeypatch.setattr(bi_sso.httpx, "AsyncClient", _mock_client({}))
    for role in ("ADMIN", "MANAGER", "DIRECTOR"):
        u = CurrentUser(user_id=1, role=role, dept_id=None, license_flag=False)
        resp = await bi_sso.metabase_login(user=u)
        assert resp.status_code == 302
