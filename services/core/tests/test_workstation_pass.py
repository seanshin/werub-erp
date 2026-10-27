"""재고 워크스테이션 일일 출입증 — PIN 발급·로그인·폐기 + WORKSTATION 권한 제한."""
import pytest

from erp.common.policy import Role, ScopeKind, resolve_scope
from erp.common.workstation_pass_service import WorkstationPassService
from erp.errors import TokenError

svc = WorkstationPassService()


def _req(path: str, method: str = "GET"):
    from types import SimpleNamespace
    return SimpleNamespace(url=SimpleNamespace(path=path), method=method)


async def test_create_and_login(session):
    wp, pin = await svc.create(session, actor=1, label="김알바 오전")
    assert len(pin) == 6 and pin.isdigit()
    out = await svc.login(session, pin=pin)
    assert out["access_token"] and out["valid_until"]
    import jwt
    from erp.config import get_settings
    payload = jwt.decode(out["access_token"], get_settings().jwt_secret, algorithms=["HS256"])
    assert payload["role"] == "WORKSTATION" and payload["sub"] == str(wp.id)


async def test_wrong_pin_rejected(session):
    await svc.create(session, actor=1)
    with pytest.raises(TokenError):
        await svc.login(session, pin="000000")


async def test_revoked_pass_cannot_login(session):
    wp, pin = await svc.create(session, actor=1)
    await svc.revoke(session, pass_id=wp.id)
    with pytest.raises(TokenError):
        await svc.login(session, pin=pin)


def test_workstation_role_scope_limited():
    def kind(res, act):
        return resolve_scope(res, act, role=Role.WORKSTATION, dept_id=None, user_id=1).kind
    # 허용: 재고 작업(입고·라벨·마약류 수불)
    assert kind("scm.items", "read") == ScopeKind.ALL
    assert kind("scm.items", "write") == ScopeKind.ALL
    assert kind("scm.narcotics", "write") == ScopeKind.ALL
    # 차단: 구매발주·계약·결재(보안검토 2026-06-29 — 키오스크 PO·전권 우회 차단) + 전체 ERP
    assert kind("scm.po", "read") == ScopeKind.NONE
    assert kind("scm.po", "write") == ScopeKind.NONE
    assert kind("finance.statements", "read") == ScopeKind.NONE
    assert kind("hr.employees", "read") == ScopeKind.NONE
    assert kind("admin.users", "read") == ScopeKind.NONE


async def test_revoked_pass_token_rejected_at_auth(session):
    """S2: 발급된 WORKSTATION 토큰도 폐기 즉시 무효 — get_current_user가 매 요청 DB 확인."""
    from erp.common.router import get_current_user
    wp, pin = await svc.create(session, actor=1)
    token = (await svc.login(session, pin=pin))["access_token"]
    cu = await get_current_user(request=_req("/api/v1/inventory/items"),
                                authorization=f"Bearer {token}", access_token=None, session=session)
    assert cu.role == "WORKSTATION"            # 폐기 전 통과
    await svc.revoke(session, pass_id=wp.id)
    with pytest.raises(TokenError):            # 폐기 후 같은 토큰 차단
        await get_current_user(request=_req("/api/v1/inventory/items"),
                               authorization=f"Bearer {token}", access_token=None, session=session)


async def test_workstation_cannot_act_as_same_numbered_user(session, client):
    """전체 흐름 검토 G(CRITICAL) — 출입증 id=N 토큰이 user N으로 행세하던 경로 차단.
    본인용 EP(PIN·패스키·me·알림·급여명세)는 403, 재고 경로만 통과."""
    from erp.common.models import User
    wp, pin = await svc.create(session, actor=1)
    victim = User(id=wp.id, email="victim@x.example", role="ADMIN", password_hash="x", is_active=True,
                  token_version=0)
    session.add(victim)
    await session.flush()
    h = {"Authorization": "Bearer " + (await svc.login(session, pin=pin))["access_token"]}
    for method, path, body in [("post", "/api/v1/auth/pin", {"pin": "123456"}),
                               ("post", "/api/v1/auth/passkeys/register/begin", {}),
                               ("get", "/api/v1/auth/me", None),
                               ("get", "/api/v1/notifications", None),
                               ("post", "/api/v1/auth/logout", None),
                               ("get", "/api/v1/hr/leaves/remaining", None)]:
        r = await getattr(client, method)(path, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.status_code, r.text)
    await session.refresh(victim)
    assert victim.pin_hash is None
    ok = await client.get("/api/v1/inventory/items", headers=h)
    assert ok.status_code == 200, ok.text


async def test_workstation_cannot_issue_passes_or_change_costing(session, client):
    """2차 검토 G — 허용 경로를 작업대가 실제 부르는 EP로 한정(출입증 발급·폐기 차단). user_id는 음수."""
    from erp.common.router import get_current_user
    wp, pin = await svc.create(session, actor=1)
    token = (await svc.login(session, pin=pin))["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    for path in ("/api/v1/inventory/workstation/passes", "/api/v1/inventory/nrv/post",
                 "/api/v1/inventory/items"):              # items는 GET만(POST=마스터 생성)
        r = await client.post(path, headers=h, json={})
        assert r.status_code == 403, (path, r.status_code)
    cu = await get_current_user(request=_req("/api/v1/inventory/stock-takes/3/apply", "POST"),
                                authorization=f"Bearer {token}", access_token=None, session=session)
    assert cu.user_id == -wp.id
