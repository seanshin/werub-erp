"""HIS SSO 정책·랜딩 우선순위·로그 가림·ENV 검증."""
import logging
import uuid

import pytest
from sqlalchemy import select

from erp.common.models import OrgSettings, User
from erp.common.org_service import set_sso_policy, sso_policy
from erp.common.sso_service import HisSsoService, SsoTokenModeBlockedError
from erp.errors import AuthFailedError
from tests.test_sso import HIS_SECRET, _FakeRedis, _his_token


@pytest.fixture
def his_secret():
    from erp.config import get_settings
    s = get_settings()
    old, s.his_jwt_secret = s.his_jwt_secret, HIS_SECRET
    yield HIS_SECRET
    s.his_jwt_secret = old


async def _org(session):
    session.add(OrgSettings(id=1, name="테스트병원"))
    await session.flush()


# ── 정책 ────────────────────────────────────────────────────────

async def test_undecided_policy_keeps_legacy_behavior(session, his_secret):
    """미결정이면 종전 동작(token 허용·aud 미검증) + 미결정 항목을 드러낸다."""
    await _org(session)
    pol = await sso_policy(session)
    assert pol["token_allowed"] is True and pol["audience"] is None
    assert set(pol["undecided"]) == {"token_mode", "enforce_audience"}
    out = await HisSsoService().login_with_his_token(
        session, his_token=_his_token("legacy@his.example", "STAFF"))
    assert out["access_token"]


async def test_token_mode_block_rejects_token_but_not_cookie(session, his_secret):
    """token 차단 결정 → URL/본문 토큰은 거부, 동일호스트 cookie 핸드오프는 그대로."""
    await _org(session)
    await set_sso_policy(session, token_mode="block", enforce_audience=None, actor=1)
    svc = HisSsoService()
    with pytest.raises(SsoTokenModeBlockedError, match="HIS 관리자"):
        await svc.login_with_his_token(session, his_token=_his_token("t@his.example", "STAFF"))
    out = await svc.login_with_his_token(
        session, his_token=_his_token("t@his.example", "STAFF"), mode="cookie")
    assert out["access_token"]


async def test_enforce_audience_requires_erp_sso_aud_and_jti(session, his_secret):
    """aud 강제 → HIS 메인 토큰(aud 없음·jti 없음) 거부, erp-sso+jti 토큰만 통과."""
    await _org(session)
    pol = await set_sso_policy(session, token_mode=None, enforce_audience=True, actor=1)
    assert pol["audience"] == "erp-sso" and pol["audience_source"] == "db"
    svc = HisSsoService(redis=_FakeRedis())
    with pytest.raises(AuthFailedError):      # aud 없음
        await svc.login_with_his_token(session, his_token=_his_token("a@his.example", "STAFF"))
    with pytest.raises(AuthFailedError):      # aud는 맞지만 jti 없음 → 반복 수락 가능 토큰
        await svc.login_with_his_token(
            session, his_token=_his_token("a@his.example", "STAFF", aud="erp-sso"))
    out = await svc.login_with_his_token(
        session, his_token=_his_token("a@his.example", "STAFF", aud="erp-sso", jti="h-1"),
        mode="cookie")
    assert out["access_token"]


async def test_env_audience_wins_and_is_not_undecided(session, his_secret):
    from erp.config import get_settings
    await _org(session)
    s = get_settings()
    old, s.his_jwt_audience = s.his_jwt_audience, "erp-sso"
    try:
        pol = await sso_policy(session)
        assert pol["audience_source"] == "env" and "enforce_audience" not in pol["undecided"]
    finally:
        s.his_jwt_audience = old


async def test_sso_policy_endpoint_registered_and_rbac(session, client):
    from tests._http import auth_as
    await _org(session)
    admin = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.put("/api/v1/admin/integrations/sso-policy", headers=admin,
                         json={"token_mode": "block", "enforce_audience": True})
    assert r.status_code == 200, r.text
    assert r.json()["token_allowed"] is False and r.json()["undecided"] == []
    g = await client.get("/api/v1/admin/integrations/sso-policy", headers=admin)
    assert g.status_code == 200 and g.json()["audience"] == "erp-sso"
    bad = await client.put("/api/v1/admin/integrations/sso-policy", headers=admin,
                           json={"token_mode": "maybe"})
    assert bad.status_code == 422
    staff = await auth_as(session, user_id=2, role="STAFF")
    assert (await client.put("/api/v1/admin/integrations/sso-policy", headers=staff,
                             json={"token_mode": "allow"})).status_code == 403


# ── 랜딩 우선순위 ──────────────────────────────────────────

def _cookies(resp) -> list[str]:
    return resp.headers.get_list("set-cookie")


async def _session_cookie_for(session, email: str) -> str:
    out = await HisSsoService().login_with_his_token(
        session, his_token=_his_token(email, "STAFF"), mode="cookie")
    return out["access_token"]


async def test_landing_handoff_replaces_existing_session(session, client, his_secret):
    """HIS에서 다른 사용자로 바꿔 누르면 ERP 세션도 그 사용자로 바뀐다(종전: 이전 사용자 유지)."""
    from erp.common.security import decode_access_token
    await _org(session)
    old = await _session_cookie_for(session, "nurse-a@his.example")
    client.cookies.set("access_token", old)
    client.cookies.set("his_sso", _his_token("nurse-b@his.example", "STAFF", jti=uuid.uuid4().hex))
    r = await client.get("/", follow_redirects=False)
    client.cookies.clear()
    assert r.status_code == 302 and r.headers["location"].endswith("/dashboard")
    new = next(c for c in _cookies(r) if c.startswith("access_token="))
    tok = new.split("=", 1)[1].split(";", 1)[0]
    b = (await session.execute(select(User).where(User.email == "nurse-b@his.example"))).scalar_one()
    assert decode_access_token(tok)["sub"] == str(b.id)


async def test_landing_failed_handoff_clears_existing_session(session, client, his_secret):
    """핸드오프가 실패하면 기존 세션으로 들여보내지 않는다 — 누구의 세션인지 보장이 없다."""
    await _org(session)
    old = await _session_cookie_for(session, "nurse-a@his.example")
    client.cookies.set("access_token", old)
    client.cookies.set("his_sso", _his_token("x@his.example", "STAFF", secret="wrong-secret-" * 3))
    r = await client.get("/", follow_redirects=False)
    client.cookies.clear()
    assert r.headers["location"].endswith("/login")
    assert any(c.startswith("access_token=") and "max-age=0" in c.lower() for c in _cookies(r))
    assert any(c.startswith("refresh_token=") and "max-age=0" in c.lower() for c in _cookies(r))


async def test_landing_session_without_handoff_goes_dashboard(session, client, his_secret):
    await _org(session)
    client.cookies.set("access_token", await _session_cookie_for(session, "s@his.example"))
    r = await client.get("/", follow_redirects=False)
    client.cookies.clear()
    assert r.headers["location"].endswith("/dashboard")


# ── 접근 로그 가림 ────────────────────────────────────────

def test_access_log_redacts_query_token():
    from erp.logging import AccessLogRedactFilter
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                            '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET",
                            "/api/v1/auth/sso/his?token=eyJhbGci.abc.def&x=1", "1.1", 302),
                            None)
    AccessLogRedactFilter().filter(rec)
    line = rec.getMessage()
    assert "eyJhbGci" not in line and "token=***&x=1" in line


def test_setup_logging_installs_access_filter():
    from erp.logging import AccessLogRedactFilter, setup_logging
    setup_logging()
    setup_logging()   # 중복 설치 안 함
    n = sum(isinstance(f, AccessLogRedactFilter)
            for f in logging.getLogger("uvicorn.access").filters)
    assert n == 1


# ── ENV 검증 ───────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["production", "staging", "", "prd"])
def test_unknown_env_refuses_startup(bad):
    from pydantic import ValidationError as PydValidationError

    from erp.config import Settings
    with pytest.raises(PydValidationError, match="알 수 없는 ENV"):
        Settings(env=bad)


def test_env_is_normalized():
    from erp.config import Settings
    assert Settings(env=" PROD ").env == "prod"


# ── v1.288 흐름검토 G2·G3·G4 ────────────────────────────────────

async def test_failed_handoff_clears_cookies_even_without_valid_session(session, client, his_secret):
    """G2 — 액세스 토큰이 만료돼도(세션 없음 판정) 이전 사용자의 refresh 쿠키를 지운다."""
    await _org(session)
    client.cookies.set("his_sso", _his_token("x@his.example", "STAFF", secret="wrong-secret-" * 3))
    r = await client.get("/", follow_redirects=False)
    client.cookies.clear()
    cookies = _cookies(r)
    assert r.headers["location"].endswith("/login")
    for name in ("access_token=", "refresh_token=", "his_sso="):
        assert any(c.startswith(name) and "max-age=0" in c.lower() for c in cookies), name


async def test_replay_passes_only_for_same_user(session, his_secret):
    """G3 — 재사용 토큰은 그 토큰으로 세션을 받은 사용자를 알려 준다(랜딩이 대조)."""
    from erp.common.sso_service import SsoReplayError
    svc = HisSsoService(redis=_FakeRedis())
    tok = _his_token("g3@his.example", "STAFF", jti=uuid.uuid4().hex)
    await svc.login_with_his_token(session, his_token=tok, mode="cookie")
    uid = (await session.execute(select(User.id).where(User.email == "g3@his.example"))).scalar_one()
    with pytest.raises(SsoReplayError) as ei:
        await svc.login_with_his_token(session, his_token=tok, mode="cookie")
    assert ei.value.consumed_by == str(uid)


async def test_replay_after_failed_first_request_has_no_consumer(session, his_secret):
    """첫 요청이 실패(비활성 계정)하면 소비자 없음 — 랜딩은 남은 세션으로 들여보내지 않는다."""
    from erp.common.sso_service import SsoReplayError
    session.add(User(email="gone@his.example", role="STAFF", password_hash="x", is_active=False))
    await session.flush()
    svc = HisSsoService(redis=_FakeRedis())
    tok = _his_token("gone@his.example", "STAFF", jti=uuid.uuid4().hex)
    with pytest.raises(AuthFailedError):
        await svc.login_with_his_token(session, his_token=tok, mode="cookie")
    with pytest.raises(SsoReplayError) as ei:
        await svc.login_with_his_token(session, his_token=tok, mode="cookie")
    assert ei.value.consumed_by is None


async def test_email_case_does_not_bypass_deactivation(session, his_secret):
    """G4 — 대소문자만 다른 이메일로 새 활성 계정이 생기지 않는다(차단 우회)."""
    session.add(User(email="case@his.example", role="STAFF", password_hash="x", is_active=False))
    await session.flush()
    with pytest.raises(AuthFailedError, match="비활성"):
        await HisSsoService().login_with_his_token(
            session, his_token=_his_token("Case@HIS.example", "STAFF"))
    n = (await session.execute(select(User).where(User.email.ilike("case@his.example")))).scalars().all()
    assert len(n) == 1


# ── 2차 검토 H1·H2·H3 ───────────────────────────────────────────

async def test_resigned_employee_cannot_sso_in(session, his_secret):
    """H1 — 퇴사일이 지난 직원은 SSO(JIT 포함)로 들어오지 못한다."""
    from datetime import date, timedelta

    from erp.common.kst import kst_today
    from erp.hr.models import Employee
    session.add(Employee(employee_no="Q-1", name="q", department_id=1, hired_on=date(2020, 1, 1),
                         job_group="NURSE", email="quit@his.example",
                         resigned_on=kst_today() - timedelta(days=2)))
    await session.flush()
    with pytest.raises(AuthFailedError, match="퇴사"):
        await HisSsoService().login_with_his_token(session, his_token=_his_token("quit@his.example", "STAFF"))


async def test_case_duplicates_fail_closed(session, his_secret):
    """H2 — 대소문자만 다른 행 중 하나라도 비활성이면 거부(행 선택 순서와 무관)."""
    session.add_all([User(email="Dup@his.example", role="STAFF", password_hash="x", is_active=True),
                     User(email="dup@his.example", role="STAFF", password_hash="x", is_active=False)])
    await session.flush()
    with pytest.raises(AuthFailedError, match="중복 계정"):
        await HisSsoService().login_with_his_token(session, his_token=_his_token("dup@his.example", "STAFF"))


async def test_replay_consumed_by_other_does_not_clear_cookies(session, client, his_secret, monkeypatch):
    """H3 — 첫 요청이 성공한 동시 이중 요청: 두 번째 응답은 세션 쿠키를 지우지 않고 랜딩을 다시 탄다."""
    from erp.common import sso_service

    async def fake_login(self, session, *, his_token, mode="token"):
        raise sso_service.SsoReplayError("999")
    monkeypatch.setattr(sso_service.HisSsoService, "login_with_his_token", fake_login)
    client.cookies.set("his_sso", "x")
    r = await client.get("/", follow_redirects=False)
    assert r.headers["location"].endswith("/?replay=1")
    assert not _cookies(r)                                  # 첫 응답이 심을 세션을 지우지 않는다
    # 재진입에도 첫 응답이 안 왔으면(his_sso 그대로·다른 사용자) — 이번엔 지우고 로그인으로(I3)
    r2 = await client.get("/?replay=1", follow_redirects=False)
    client.cookies.clear()
    assert r2.headers["location"].endswith("/login")
    assert any(c.startswith("refresh_token=") and "max-age=0" in c.lower() for c in _cookies(r2))


# ── 3차 검토 I1 ────────────────────────────────────────────────

async def _resigned_emp(session, email, *, revoked_on=None):
    from datetime import date, timedelta

    from erp.common.kst import kst_today
    from erp.hr.models import Employee
    e = Employee(employee_no="I1-" + email[:3], name="i", department_id=1, hired_on=date(2020, 1, 1),
                 job_group="NURSE", email=email, resigned_on=kst_today() - timedelta(days=5),
                 access_revoked_on=revoked_on)
    session.add(e)
    await session.flush()
    return e


async def test_admin_reenabled_resigned_account_can_sso(session, his_secret):
    """I1 — 차단 잡이 처리한 퇴사(표식 있음) 뒤 관리자가 다시 켠 계정은 SSO 허용(is_active가 정본)."""
    from erp.common.kst import kst_today
    e = await _resigned_emp(session, "adv@his.example", revoked_on=kst_today())
    session.add(User(email="adv@his.example", role="STAFF", password_hash="x", is_active=True,
                     employee_id=e.id))
    await session.flush()
    out = await HisSsoService().login_with_his_token(session, his_token=_his_token("adv@his.example", "STAFF"))
    assert out["access_token"]


async def test_resigned_unprocessed_existing_account_rejected(session, his_secret):
    """아직 차단 잡이 처리 전(표식 없음)인 퇴사자 기존 계정은 거부 — 다음 실행 전 틈."""
    e = await _resigned_emp(session, "pre@his.example")
    session.add(User(email="pre@his.example", role="STAFF", password_hash="x", is_active=True,
                     employee_id=e.id))
    await session.flush()
    with pytest.raises(AuthFailedError, match="퇴사"):
        await HisSsoService().login_with_his_token(session, his_token=_his_token("pre@his.example", "STAFF"))


async def test_resigned_no_jit_account_created(session, his_secret):
    """퇴사자 JIT 신규 생성 거부 — 거부 뒤 계정 행도 남지 않는다(랜딩이 커밋해도)."""
    from erp.common.kst import kst_today
    await _resigned_emp(session, "newq@his.example", revoked_on=kst_today())
    with pytest.raises(AuthFailedError, match="퇴사"):
        await HisSsoService().login_with_his_token(session, his_token=_his_token("newq@his.example", "STAFF"))
    assert (await session.execute(select(User).where(User.email == "newq@his.example"))).first() is None


async def test_retire_duplicate_restores_sso(session, client, his_secret):
    """I2 — 대소문자 중복 행을 관리화면에서 정리하면 진짜 사용자의 SSO가 다시 된다(비활성화만으론 막힘)."""
    from tests._http import auth_as
    admin = await auth_as(session, user_id=1, role="ADMIN")
    real = User(email="real@his.example", role="STAFF", password_hash="x", is_active=True)
    dup = User(email="Real@HIS.example", role="STAFF", password_hash="x", is_active=False)
    session.add_all([real, dup])
    await session.flush()
    lst = await client.get("/api/v1/admin/users", headers=admin)
    flags = {u["id"]: u["email_duplicate"] for u in lst.json()["items"]}
    assert flags[real.id] and flags[dup.id]
    with pytest.raises(AuthFailedError, match="중복"):
        await HisSsoService().login_with_his_token(session, his_token=_his_token("real@his.example", "STAFF"))
    r = await client.post(f"/api/v1/admin/users/{dup.id}/retire-duplicate", headers=admin)
    assert r.status_code == 200, r.text
    out = await HisSsoService().login_with_his_token(session, his_token=_his_token("real@his.example", "STAFF"))
    assert out["access_token"]
    # 단독 계정엔 쓸 수 없다
    bad = await client.post(f"/api/v1/admin/users/{real.id}/retire-duplicate", headers=admin)
    assert bad.status_code == 422


# ── 4차 검토 D1·D2·R1 ───────────────────────────────────────────

async def test_rehired_employee_not_blocked_by_old_row(session, his_secret):
    """D2 — 같은 이메일의 옛 퇴사 행이 있어도 재직 중 행이 있으면 JIT·로그인 허용, 새 행에 연결."""
    from datetime import date, timedelta

    from erp.common.kst import kst_today
    from erp.hr.models import Employee
    old = Employee(employee_no="RH-1", name="r", department_id=1, hired_on=date(2018, 1, 1),
                   job_group="NURSE", email="rehire@his.example", resigned_on=kst_today() - timedelta(days=400))
    new = Employee(employee_no="RH-2", name="r", department_id=1, hired_on=date(2026, 1, 1),
                   job_group="NURSE", email="rehire@his.example")
    session.add_all([old, new])
    await session.flush()
    out = await HisSsoService().login_with_his_token(session, his_token=_his_token("rehire@his.example", "STAFF"))
    assert out["access_token"]
    u = (await session.execute(select(User).where(User.email == "rehire@his.example"))).scalar_one()
    assert u.employee_id == new.id


async def test_unlinked_account_cannot_borrow_revoke_marker(session, his_secret):
    """R1 — 연결 없는 계정이 이메일로만 퇴사자와 맞으면 표식이 있어도 거부."""
    from erp.common.kst import kst_today
    await _resigned_emp(session, "borrow@his.example", revoked_on=kst_today())
    session.add(User(email="borrow@his.example", role="STAFF", password_hash="x", is_active=True))
    await session.flush()
    with pytest.raises(AuthFailedError, match="퇴사"):
        await HisSsoService().login_with_his_token(session, his_token=_his_token("borrow@his.example", "STAFF"))


async def test_retire_duplicate_refuses_if_resigned_active_remains(session, client):
    """D1 — 차단된 행을 치워 남는 활성 행이 (미처리) 퇴사자면 정리 거부. 판정은 SSO와 같은 함수."""
    from tests._http import auth_as
    admin = await auth_as(session, user_id=1, role="ADMIN")
    e = await _resigned_emp(session, "d1@his.example")                # 차단 잡 처리 전
    blocked = User(email="d1@his.example", role="STAFF", password_hash="x", is_active=False, employee_id=e.id)
    bypass = User(email="D1@his.example", role="STAFF", password_hash="x", is_active=True, employee_id=e.id)
    session.add_all([blocked, bypass])
    await session.flush()
    r = await client.post(f"/api/v1/admin/users/{blocked.id}/retire-duplicate", headers=admin)
    assert r.status_code == 422 and "퇴사" in r.text


async def test_retire_duplicate_allows_admin_reenabled_processed(session, client, his_secret):
    """5차 — 처리된 퇴사 + 관리자가 다시 켠 연결 계정이 남으면 정리 허용(SSO와 같은 판정·영구 차단 방지)."""
    from erp.common.kst import kst_today
    from tests._http import auth_as
    admin = await auth_as(session, user_id=1, role="ADMIN")
    e = await _resigned_emp(session, "adv2@his.example", revoked_on=kst_today())
    old = User(email="ADV2@his.example", role="STAFF", password_hash="x", is_active=False)
    keep = User(email="adv2@his.example", role="STAFF", password_hash="x", is_active=True, employee_id=e.id)
    session.add_all([old, keep])
    await session.flush()
    r = await client.post(f"/api/v1/admin/users/{old.id}/retire-duplicate", headers=admin)
    assert r.status_code == 200, r.text
    out = await HisSsoService().login_with_his_token(session, his_token=_his_token("adv2@his.example", "STAFF"))
    assert out["access_token"]


async def test_rehire_linked_to_old_row_blocked_until_admin_relinks(session, client, his_secret):
    """6차 — 옛 퇴사 행에 연결된 계정은 자동으로 옮기지 않는다(재입사 vs 공용 이메일 구분 불가).
    SSO는 조치 주체를 알려 거부 → 관리자가 사용자 관리에서 재연결·활성화 → SSO 통과."""
    from datetime import date, timedelta

    from erp.common.kst import kst_today
    from erp.hr.models import Employee
    from tests._http import auth_as
    admin = await auth_as(session, user_id=1, role="ADMIN")
    old = Employee(employee_no="RL-1", name="r", department_id=1, hired_on=date(2018, 1, 1),
                   job_group="NURSE", email="relink@his.example", resigned_on=kst_today() - timedelta(days=200))
    new = Employee(employee_no="RL-2", name="r", department_id=1, hired_on=date(2026, 1, 1),
                   job_group="NURSE", email="relink@his.example")
    session.add_all([old, new])
    await session.flush()
    u = User(email="relink@his.example", role="STAFF", password_hash="x", is_active=True, employee_id=old.id)
    session.add(u)
    await session.flush()
    with pytest.raises(AuthFailedError, match="재연결"):
        await HisSsoService().login_with_his_token(session, his_token=_his_token("relink@his.example", "STAFF"))
    assert u.employee_id == old.id                           # 자동 이동 없음
    lst = await client.get("/api/v1/admin/users", headers=admin)
    row = next(x for x in lst.json()["items"] if x["id"] == u.id)
    assert row["relink_candidate"] == new.id
    bad = await client.post(f"/api/v1/admin/users/{u.id}/relink", headers=admin,
                            json={"employee_id": old.id})
    assert bad.status_code == 422                            # 후보가 아닌 직원으로는 불가
    r = await client.post(f"/api/v1/admin/users/{u.id}/relink", headers=admin,
                          json={"employee_id": new.id})
    assert r.status_code == 200, r.text
    out = await HisSsoService().login_with_his_token(session, his_token=_his_token("relink@his.example", "STAFF"))
    assert out["access_token"]


async def test_resigned_linked_account_not_passed_by_shared_email_colleague(session, his_secret):
    """6차 결함1 — 공용 이메일의 다른 재직자가 있어도 퇴사자에 연결된 계정은 통과하지 않는다."""
    from datetime import date, timedelta

    from erp.common.kst import kst_today
    from erp.hr.models import Employee
    x = Employee(employee_no="SX-1", name="x", department_id=1, hired_on=date(2018, 1, 1),
                 job_group="NURSE", email="team@his.example", resigned_on=kst_today() - timedelta(days=3))
    y = Employee(employee_no="SY-1", name="y", department_id=1, hired_on=date(2019, 1, 1),
                 job_group="NURSE", email="team@his.example")
    session.add_all([x, y])
    await session.flush()
    u = User(email="team@his.example", role="STAFF", password_hash="x", is_active=True, employee_id=x.id)
    session.add(u)
    await session.flush()
    with pytest.raises(AuthFailedError):
        await HisSsoService().login_with_his_token(session, his_token=_his_token("team@his.example", "STAFF"))
    assert u.employee_id == x.id                             # 남의 기록(y)으로 옮겨 가지 않음


async def test_shared_email_two_active_employees_not_linked(session, his_secret):
    """5차 — 재직 행이 2명(공용 이메일)이면 임의로 연결하지 않는다."""
    from datetime import date

    from erp.hr.models import Employee
    session.add_all([Employee(employee_no=f"SH-{i}", name="s", department_id=1, hired_on=date(2020, 1, 1),
                              job_group="NURSE", email="shared@his.example") for i in (1, 2)])
    await session.flush()
    await HisSsoService().login_with_his_token(session, his_token=_his_token("shared@his.example", "STAFF"))
    u = (await session.execute(select(User).where(User.email == "shared@his.example"))).scalar_one()
    assert u.employee_id is None


async def test_inbound_does_not_link_account_to_resigned_row(session):
    """5차 — 옛 사번 staff.resigned가 와도 재입사자 계정을 퇴사 행에 연결·차단하지 않는다."""
    from datetime import date, timedelta

    from erp.common.kst import kst_today
    from erp.hr.models import Employee
    from erp.integrations.inbound_consumer import _staff
    session.add_all([Employee(employee_no="IN-OLD", name="o", department_id=1, hired_on=date(2018, 1, 1),
                              job_group="NURSE", email="back2@his.example", source="HIS"),
                     Employee(employee_no="IN-NEW", name="n", department_id=1, hired_on=date(2026, 1, 1),
                              job_group="NURSE", email="back2@his.example", source="HIS")])
    u = User(email="Back2@his.example", role="STAFF", password_hash="x", is_active=True)
    session.add(u)
    await session.flush()
    await _staff(session, {"employeeNumber": "IN-OLD", "email": "back2@his.example",
                           "resignDate": (kst_today() - timedelta(days=1)).isoformat()}, "staff.resigned", {})
    await session.refresh(u)
    assert u.is_active is True and u.employee_id is None
