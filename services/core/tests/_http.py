"""HTTP 계층 테스트 헬퍼 — User 시딩 + Bearer 토큰(get_current_user는 token_version DB 검증)."""


async def make_user(session, *, user_id: int = 1, role: str = "ADMIN",
                    dept_id: int | None = None, token_version: int = 0):
    """인증 통과용 최소 User 행. get_current_user가 User.token_version을 대조하므로 필수.

    dept_id가 주어지면 참조 Department를 자동 시딩(PG는 FK 강제·sqlite는 무시 — PG 레인 P2-7 대응)."""
    from erp.common.models import Department, User
    if dept_id is not None:
        exists = await session.get(Department, dept_id)
        if exists is None:
            session.add(Department(id=dept_id, name=f"부서{dept_id}"))
            await session.flush()
    u = User(id=user_id, email=f"u{user_id}@test.example", role=role, department_id=dept_id,
             password_hash="x", token_version=token_version, is_active=True)
    session.add(u)
    await session.flush()
    return u


def bearer(*, user_id: int = 1, role: str = "ADMIN", dept_id: int | None = None,
           token_version: int = 0, license_flag: bool = True) -> dict:
    """Authorization 헤더 — make_user와 동일 (user_id, role, token_version)로 발급."""
    from erp.common.security import create_access_token
    tok = create_access_token(user_id=user_id, role=role, dept_id=dept_id,
                              token_version=token_version, license_flag=license_flag)
    return {"Authorization": f"Bearer {tok}"}


async def auth_as(session, *, user_id: int = 1, role: str = "ADMIN",
                  dept_id: int | None = None) -> dict:
    """User 시딩 + 매칭 Bearer 헤더 반환(가장 흔한 조합)."""
    await make_user(session, user_id=user_id, role=role, dept_id=dept_id)
    return bearer(user_id=user_id, role=role, dept_id=dept_id)
