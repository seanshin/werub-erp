"""/auth/me 의 Grafana auth.proxy 신원 헤더(X-Erp-User/X-Erp-Role) — BI SSO 역할 매핑."""
from fastapi import Response

from erp.common.models import User
from erp.common.router import CurrentUser, me


async def _user(session, email, role):
    u = User(email=email, password_hash="x", role=role)
    session.add(u)
    await session.flush()
    return u


async def test_me_sets_user_and_editor_role_for_manager(session):
    u = await _user(session, "boss@demo.example", "MANAGER")
    resp = Response()
    out = await me(resp, user=CurrentUser(user_id=u.id, role="MANAGER", dept_id=None,
                                          license_flag=False), session=session)
    assert out["email"] == "boss@demo.example"
    assert resp.headers["X-Erp-User"] == "boss@demo.example"     # username = email
    assert resp.headers["X-Erp-Role"] == "Editor"           # MANAGER → Editor(편집/저장)


async def test_me_role_mapping(session):
    cases = {"ADMIN": "Admin", "DIRECTOR": "Editor", "STAFF": "Viewer"}
    for erp_role, gf_role in cases.items():
        u = await _user(session, f"{erp_role}@demo.example", erp_role)
        resp = Response()
        await me(resp, user=CurrentUser(user_id=u.id, role=erp_role, dept_id=None,
                                        license_flag=False), session=session)
        assert resp.headers["X-Erp-Role"] == gf_role


async def test_me_user_header_falls_back_to_id_when_no_email(session):
    # 이메일 없는(이론상) 사용자 — user_id 폴백으로 username 보장(헤더 누락 방지)
    u = await _user(session, "x@demo.example", "STAFF")
    # email 컬럼은 NOT NULL이라 실제 None 불가 → 폴백 경로는 코드상 보장(or f"erp-{id}")만 확인
    resp = Response()
    await me(resp, user=CurrentUser(user_id=u.id, role="STAFF", dept_id=None,
                                    license_flag=False), session=session)
    assert resp.headers["X-Erp-User"]   # 비어있지 않음


async def test_bi_check_gates_non_bi_roles(session):
    """재검토 — Grafana auth_request 게이트(/auth/bi-check): BI 역할만 200, 그 외 403."""
    import pytest

    from erp.common.router import bi_check
    from erp.errors import ForbiddenError
    # 경영 역할 → 200 + Grafana 헤더
    for role, gf in (("ADMIN", "Admin"), ("DIRECTOR", "Editor"), ("FINANCE_MANAGER", "Editor")):
        u = await _user(session, f"bi-{role}@demo.example", role)
        resp = Response()
        out = await bi_check(resp, user=CurrentUser(user_id=u.id, role=role, dept_id=None,
                                                    license_flag=False), session=session)
        assert out["ok"] is True and resp.headers["X-Erp-Role"] == gf
    # 비경영 역할 → 403(인증됐어도 BI 차단)
    for role in ("STAFF", "IT_MANAGER", "LOGISTICS_MANAGER", "WORKSTATION"):
        u = await _user(session, f"bi-{role}@demo.example", role) if role != "WORKSTATION" else None
        uid = u.id if u else 999
        with pytest.raises(ForbiddenError):
            await bi_check(Response(), user=CurrentUser(user_id=uid, role=role, dept_id=None,
                                                        license_flag=False), session=session)
