"""HIS SSO — JWT 검증·JIT 프로비저닝·역할 매핑·역할 보존 (design §6.1 확장)."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import select

from erp.common.models import User
from erp.common.sso_service import HisSsoService, map_his_role
from erp.config import get_settings
from erp.errors import AuthFailedError, ValidationError

UTC = timezone.utc
HIS_SECRET = "test-his-auth-secret-at-least-32-chars!!"
svc = HisSsoService()


@pytest.fixture
def his_secret():
    s = get_settings()
    old = s.his_jwt_secret
    s.his_jwt_secret = HIS_SECRET
    yield HIS_SECRET
    s.his_jwt_secret = old


def _his_token(email, role, secret=HIS_SECRET, exp_min=60, jti=None, aud=None):
    now = datetime.now(UTC)
    payload = {"sub": "42", "email": email, "role": role,
               "iat": now, "exp": now + timedelta(minutes=exp_min)}
    if jti:
        payload["jti"] = jti
    if aud:
        payload["aud"] = aud
    return jwt.encode(payload, secret, algorithm="HS256")


class _FakeRedis:
    """SET NX·GET 의미만 — jti 재생 테스트용(소비자 user_id 기록 포함)."""
    def __init__(self):
        self._kv: dict = {}

    async def set(self, key, val, nx=False, ex=None):
        if nx and key in self._kv:
            return None
        self._kv[key] = val
        return True

    async def get(self, key):
        return self._kv.get(key)


async def test_jti_replay_blocked(session, his_secret):
    """C-3 — 동일 jti 재사용(replay) 차단."""
    svc2 = HisSsoService(redis=_FakeRedis())
    tok = _his_token("j@his.example", "ADMIN", jti="handoff-uuid-1")
    assert (await svc2.login_with_his_token(session, his_token=tok))["access_token"]  # 1회 OK
    with pytest.raises(AuthFailedError, match="재생"):
        await svc2.login_with_his_token(session, his_token=tok)  # 동일 jti → 차단


async def test_aud_enforced_when_configured(session, his_secret):
    """C-2 — HIS_JWT_AUDIENCE 설정 시 aud 강제(토큰 confusion 차단)."""
    s = get_settings()
    old = s.his_jwt_audience
    s.his_jwt_audience = "erp-sso"
    try:
        with pytest.raises(AuthFailedError):  # aud 없는 토큰 거부
            await svc.login_with_his_token(session, his_token=_his_token("a@his.example", "ADMIN"))
        # aud 강제 시 jti도 필수. HIS erp-sso 토큰은 항상 jti.
        with pytest.raises(AuthFailedError):
            await svc.login_with_his_token(
                session, his_token=_his_token("a@his.example", "ADMIN", aud="erp-sso"))
        out = await svc.login_with_his_token(
            session, his_token=_his_token("a@his.example", "ADMIN", aud="erp-sso", jti="j-aud"))
        assert out["access_token"]
    finally:
        s.his_jwt_audience = old


def test_role_mapping():
    assert map_his_role("ADMIN") == "ADMIN"
    assert map_his_role("admin") == "ADMIN"
    assert map_his_role("DOCTOR") == "STAFF"
    assert map_his_role(None) == "STAFF"


async def test_sso_jit_creates_staff(session, his_secret):
    out = await svc.login_with_his_token(session, his_token=_his_token("doc@his.example", "DOCTOR"))
    assert out["access_token"] and out["token_type"] == "bearer" and out["refresh_token"]
    u = (await session.execute(select(User).where(User.email == "doc@his.example"))).scalar_one()
    assert u.role == "STAFF" and u.is_active


async def test_sso_admin_maps_admin(session, his_secret):
    await svc.login_with_his_token(session, his_token=_his_token("a@his.example", "ADMIN"))
    u = (await session.execute(select(User).where(User.email == "a@his.example"))).scalar_one()
    assert u.role == "ADMIN"


async def test_sso_existing_role_preserved(session, his_secret):
    """수동 DIRECTOR 승격 사용자는 SSO가 역할을 덮어쓰지 않음."""
    session.add(User(email="boss@his.example", role="DIRECTOR", password_hash="x", is_active=True))
    await session.flush()
    await svc.login_with_his_token(session, his_token=_his_token("boss@his.example", "DOCTOR"))
    u = (await session.execute(select(User).where(User.email == "boss@his.example"))).scalar_one()
    assert u.role == "DIRECTOR"


async def test_sso_invalid_signature_rejected(session, his_secret):
    bad = _his_token("x@his.example", "ADMIN", secret="wrong-secret-wrong-secret-wrong!!")
    with pytest.raises(AuthFailedError):
        await svc.login_with_his_token(session, his_token=bad)


async def test_sso_expired_rejected(session, his_secret):
    with pytest.raises(AuthFailedError):
        await svc.login_with_his_token(session, his_token=_his_token("x@his.example", "ADMIN", exp_min=-5))


async def test_sso_unconfigured_rejected(session):
    """HIS_JWT_SECRET 미설정 → 명시적 거부 (기본 비활성)."""
    assert get_settings().his_jwt_secret is None
    with pytest.raises(ValidationError, match="HIS SSO가 구성되지"):
        await svc.login_with_his_token(session, his_token="whatever")


# ── 브라우저 랜딩(GET 리시버) + 쿠키 인증 폴백 ──────────────
def _req(headers: dict):
    from starlette.requests import Request
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/",
                    "scheme": "http", "server": ("test", 80), "headers": raw})


def test_landing_html_authed_links_dashboard():
    from erp.main import _landing_html
    assert "/erp/dashboard" in _landing_html("✅ 로그인됨", "/erp", authed=True)
    assert "/erp/api/docs" in _landing_html("안내", "/erp", authed=False)


def test_external_base_uses_forwarded_headers():
    from erp.common.router import external_base
    req = _req({"host": "his.example.org", "x-forwarded-proto": "https",
               "x-forwarded-prefix": "/erp"})
    assert external_base(req) == "https://his.example.org/erp"


async def test_sso_landing_redirects_reachable_with_prefix(session, his_secret):
    """프록시 뒤 — 도달 가능한 URL(프리픽스 반영)로 302 + 프리픽스 쿠키 경로."""
    from erp.common.router import sso_his_landing
    req = _req({"host": "his.example.org", "x-forwarded-proto": "https",
               "x-forwarded-prefix": "/erp"})
    resp = await sso_his_landing(token=_his_token("land@his.example", "ADMIN"),
                                 request=req, session=session)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://his.example.org/erp/dashboard"
    cookies = [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"]
    assert any(c.startswith("access_token=") and "httponly" in c.lower() for c in cookies)
    assert any(c.startswith("refresh_token=") and "Path=/erp/api/v1/auth" in c for c in cookies)


async def test_his_sso_cookie_exchange(session, his_secret):
    """방식 C — his_sso 쿠키 소비 → ERP 세션 쿠키 발급 + his_sso 삭제(1회 소비)."""
    from erp.common.router import exchange_his_sso
    req = _req({"host": "his.example.org", "x-forwarded-proto": "https",
               "x-forwarded-prefix": "/erp"})
    resp = await exchange_his_sso(session, _his_token("c-user@his.example", "ADMIN"), req)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://his.example.org/erp/dashboard"
    cookies = [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"]
    assert any(c.startswith("access_token=") and "httponly" in c.lower() for c in cookies)
    # his_sso는 Max-Age=0으로 삭제(1회 소비)
    assert any(c.startswith("his_sso=") and ("Max-Age=0" in c or "max-age=0" in c.lower())
               for c in cookies)
    u = (await session.execute(select(User).where(User.email == "c-user@his.example"))).scalar_one()
    assert u.role == "ADMIN"


async def test_landing_redirect_is_not_cacheable(client):
    """랜딩 302는 **캐시 금지**여야 한다.

    같은 `/erp/` 가 쿠키 유무에 따라 대시보드로도 로그인으로도 간다. 캐시되면 브라우저가
    오리진에 묻지 않고 저장된 리다이렉트를 재생해, HIS 핸드오프 쿠키가 소비될 기회 자체가
    사라진다(2026-09-09 실제 증상: 클릭해도 서버에 랜딩 요청조차 오지 않았다).
    """
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/login")
    assert "no-store" in resp.headers.get("cache-control", "")


async def test_refresh_via_cookie_renews_session(session, his_secret):
    """쿠키 세션 — refresh_token 쿠키로 회전 시 새 토큰을 다시 쿠키로 갱신."""
    from fastapi import Response

    from erp.common.router import refresh
    issued = await svc.login_with_his_token(session, his_token=_his_token("r@his.example", "ADMIN"))
    req = _req({"host": "his.example.org", "x-forwarded-prefix": "/erp"})
    resp = Response()
    out = await refresh(request=req, response=resp, body=None,
                        refresh_token=issued["refresh_token"], session=session)
    assert out["access_token"] and out["refresh_token"] != issued["refresh_token"]  # 회전
    cookies = [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"]
    assert any(c.startswith("access_token=") for c in cookies)  # 쿠키로 갱신


async def test_logout_clears_cookies(session, his_secret):
    from fastapi import Response

    from erp.common.router import CurrentUser, logout
    await svc.login_with_his_token(session, his_token=_his_token("lo@his.example", "ADMIN"))
    u = (await session.execute(select(User).where(User.email == "lo@his.example"))).scalar_one()
    req = _req({"host": "his.example.org", "x-forwarded-prefix": "/erp"})
    resp = Response()
    await logout(request=req, response=resp,
                 user=CurrentUser(user_id=u.id, role="ADMIN", dept_id=None, license_flag=True),
                 session=session)
    cookies = [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"]
    assert any(c.startswith("access_token=") and "max-age=0" in c.lower() for c in cookies)


# ── 보안 하드닝 (H-1·H-4·C-1) ───────────────────────────────
def test_external_base_rejects_untrusted_host():
    from erp.common.router import external_base
    s = get_settings()
    old = s.trusted_hosts
    s.trusted_hosts = "his.example.org"
    try:
        evil = _req({"host": "evil.com", "x-forwarded-proto": "https",
                     "x-forwarded-prefix": "/erp"})
        assert external_base(evil) == s.web_origin.rstrip("/")  # 미신뢰 → web_origin 강제
        ok = _req({"host": "his.example.org", "x-forwarded-proto": "https",
                   "x-forwarded-prefix": "/erp"})
        assert external_base(ok) == "https://his.example.org/erp"
    finally:
        s.trusted_hosts = old


async def test_sso_email_domain_allowlist(session, his_secret):
    s = get_settings()
    old = s.sso_allowed_email_domains
    s.sso_allowed_email_domains = "hospital.example.org"
    try:
        with pytest.raises(AuthFailedError, match="허용되지 않은 이메일"):
            await svc.login_with_his_token(session, his_token=_his_token("x@evil.example", "ADMIN"))
        out = await svc.login_with_his_token(session, his_token=_his_token("ok@hospital.example.org", "ADMIN"))
        assert out["access_token"]
    finally:
        s.sso_allowed_email_domains = old


def test_decode_access_token_requires_exp():
    from erp.common.security import decode_access_token
    from erp.errors import TokenError
    no_exp = jwt.encode({"sub": "1", "tv": 0, "iat": 0},
                        get_settings().jwt_secret, algorithm="HS256")
    with pytest.raises(TokenError):
        decode_access_token(no_exp)


def test_csrf_double_submit_token():
    """H-2 — csrf_token 쿠키 ↔ X-CSRF-Token 헤더 일치 검증(상태변경)."""
    from erp.common.router import _csrf_check
    from erp.errors import ForbiddenError
    ok = _req({"cookie": "csrf_token=abc123", "x-csrf-token": "abc123"})
    ok.scope["method"] = "POST"
    _csrf_check(ok)  # 일치 → 통과
    bad = _req({"cookie": "csrf_token=abc123", "x-csrf-token": "WRONG"})
    bad.scope["method"] = "POST"
    with pytest.raises(ForbiddenError, match="CSRF 토큰"):
        _csrf_check(bad)
    missing = _req({"cookie": "csrf_token=abc123"})  # 헤더 누락
    missing.scope["method"] = "POST"
    with pytest.raises(ForbiddenError):
        _csrf_check(missing)


def test_session_cookies_include_csrf(his_secret):
    """set_session_cookies가 csrf_token(비 httpOnly) 쿠키도 발급."""
    from fastapi import Response

    from erp.common.router import set_session_cookies
    resp = Response()
    set_session_cookies(resp, {"access_token": "a", "refresh_token": "r"},
                        _req({"host": "his.example.org", "x-forwarded-prefix": "/erp"}))
    cookies = [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"]
    csrf = [c for c in cookies if c.startswith("csrf_token=")]
    assert csrf and "httponly" not in csrf[0].lower()  # JS 읽기 가능해야


async def test_auth_me_returns_user(session, his_secret):
    from fastapi import Response

    from erp.common.router import CurrentUser, me
    await svc.login_with_his_token(session, his_token=_his_token("me@his.example", "ADMIN"))
    u = (await session.execute(select(User).where(User.email == "me@his.example"))).scalar_one()
    out = await me(Response(), user=CurrentUser(user_id=u.id, role="ADMIN", dept_id=None,
                                                license_flag=True), session=session)
    assert out["email"] == "me@his.example" and out["role"] == "ADMIN"


async def test_cookie_auth_fallback(session, his_secret):
    """Authorization 헤더 없이 access_token 쿠키로 인증 (SSO 브라우저 세션, GET)."""
    from erp.common.router import get_current_user
    tokens = await svc.login_with_his_token(session, his_token=_his_token("c@his.example", "ADMIN"))
    user = await get_current_user(request=_req({"host": "his.example.org"}), authorization="",
                                  access_token=tokens["access_token"], session=session)
    assert user.role == "ADMIN"


async def test_csrf_blocks_cross_origin_cookie_mutation(session, his_secret):
    """H-2 — 쿠키 인증 + 상태변경(POST) + 교차 출처 Origin → 차단. 동일 출처는 통과."""
    from erp.common.router import get_current_user
    from erp.errors import ForbiddenError
    s = get_settings()
    old = s.trusted_hosts
    s.trusted_hosts = "his.example.org"
    try:
        tokens = await svc.login_with_his_token(session, his_token=_his_token("x@his.example", "ADMIN"))
        at = tokens["access_token"]
        evil = _req({"host": "his.example.org", "origin": "https://evil.com"})
        evil.scope["method"] = "POST"
        with pytest.raises(ForbiddenError):
            await get_current_user(request=evil, authorization="", access_token=at, session=session)
        same = _req({"host": "his.example.org", "origin": "https://his.example.org"})
        same.scope["method"] = "POST"
        u = await get_current_user(request=same, authorization="", access_token=at, session=session)
        assert u.role == "ADMIN"
        # Bearer 헤더는 CSRF 검증 제외(교차 출처여도 통과)
        bearer = _req({"host": "his.example.org", "origin": "https://evil.com"})
        bearer.scope["method"] = "POST"
        u2 = await get_current_user(request=bearer, authorization=f"Bearer {at}",
                                    access_token=None, session=session)
        assert u2.role == "ADMIN"
    finally:
        s.trusted_hosts = old
