"""첫 관리자 부트스트랩(break-glass) — `python -m erp.bootstrap_admin --email admin@기관.kr`.

ERP 계정은 HIS SSO 자동 생성(JIT)뿐이라, HIS를 세우고 서명 비밀을 공유하기 전에는 ERP를 쓸 수
없었다. 새 기관 설치·HIS 장애 시 서버 셸 접근권자가 관리자 한 명을 만들거나
승격한다. 컨테이너 셸 = 운영 신뢰 경계라 별도 인증은 두지 않고, 대신:

- 일회용 무작위 비밀번호를 **화면에만** 한 번 출력(DB엔 해시만·로그 미기록) → 첫 로그인 후 교체.
- 기존 사용자를 승격하면 token_version을 올려 기존 세션을 끊는다.
- 감사 기록(common.audit) — 누가 아니라 '부트스트랩으로' 만들어졌다는 사실을 남긴다.

사용: docker exec -it infra-core-1 python -m erp.bootstrap_admin --email admin@example.org
"""
import argparse
import asyncio
import secrets
import sys

from sqlalchemy import select


async def apply(session, email: str) -> tuple[str, str]:
    """관리자 생성·승격 — (action, 일회용 비밀번호). action: created | promoted. 커밋은 호출부."""
    from erp.audit.service import AuditService
    from erp.common import security
    from erp.common.models import User

    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("이메일 형식이 아닙니다")
    password = secrets.token_urlsafe(18)
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email, role="ADMIN", is_active=True,
                    password_hash=security.hash_password(password))
        session.add(user)
        action = "created"
    else:
        user.role = "ADMIN"
        user.is_active = True
        user.password_hash = security.hash_password(password)
        user.failed_attempts = 0
        user.locked_until = None
        action = "promoted"
    await session.flush()
    if action == "promoted":
        # 기존 세션 무효화 — refresh 폐기+tv 증가+캐시 삭제(tv만 올리면 refresh로 재발급, G1)
        from erp.common.auth_service import AuthService
        from erp.common.redis_client import get_redis
        await AuthService(redis=get_redis()).logout(session, user_id=user.id)
    await AuditService().record_access(
        session, actor=user.id, action="UPDATE", entity="common.users.bootstrap_admin",
        target_id=str(user.id), route="cli:erp.bootstrap_admin")
    return action, password


async def bootstrap(email: str) -> tuple[str, str]:
    from erp.db import session_factory
    async with session_factory()() as session:
        async with session.begin():
            return await apply(session, email)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ERP 첫 관리자 부트스트랩(break-glass)")
    ap.add_argument("--email", required=True)
    args = ap.parse_args(argv)
    action, password = asyncio.run(bootstrap(args.email))
    print(f"[{action}] {args.email} → ADMIN")
    print(f"일회용 비밀번호: {password}")
    print("⚠ 이 비밀번호는 다시 표시되지 않습니다. /erp/login 비밀번호 로그인 후 즉시 PIN·패스키를 등록하고,"
          " 운영 계정 정리 후 이 계정의 비밀번호 로그인을 쓰지 마세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
