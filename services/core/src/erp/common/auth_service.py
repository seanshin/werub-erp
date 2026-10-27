"""인증 유스케이스 — design §6.1.

- 로그인: 실패 잠금(5회/15분), argon2 검증
- refresh 회전: 원자 CAS (UPDATE ... WHERE revoked_at IS NULL RETURNING) + grace window
- 강제 무효화: token_version 증가 + Redis write-through 삭제
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common import security
from erp.common.models import RefreshToken, User
from erp.config import get_settings
from erp.errors import AccountLockedError, AuthFailedError, TokenError
from erp.common.kst import kst_iso

TV_CACHE_KEY = "user:{user_id}:tv"  # Redis token_version 캐시 (design §6.1)


class AuthService:
    def __init__(self, redis=None):
        self._redis = redis  # 없으면 DB 폴백 (테스트·로컬)

    # ── 로그인 ──────────────────────────────────────────────
    async def login(self, session: AsyncSession, *, email: str, password: str) -> dict:
        s = get_settings()
        user = (await session.execute(
            select(User).where(User.email == email, User.is_active == True)  # noqa: E712
        )).scalar_one_or_none()
        if user is None:
            security.dummy_verify(password)   # 타이밍 균일화(사용자 열거 방지)
            raise AuthFailedError()

        now = datetime.now(timezone.utc)
        if user.locked_until and user.locked_until > now:
            raise AccountLockedError(kst_iso(user.locked_until))

        if not security.verify_password(password, user.password_hash):
            user.failed_attempts += 1
            if user.failed_attempts >= s.login_max_failures:
                user.locked_until = now + timedelta(minutes=s.login_lockout_minutes)
                user.failed_attempts = 0
            raise AuthFailedError()

        user.failed_attempts = 0
        user.locked_until = None
        return await self._issue_tokens(session, user, family_id=str(uuid.uuid4()))

    # ── refresh 회전 (원자 CAS — design §6.1) ───────────────
    async def refresh(self, session: AsyncSession, *, raw_refresh: str) -> dict:
        s = get_settings()
        token_hash = security.hash_refresh_token(raw_refresh)
        now = datetime.now(timezone.utc)

        new_raw, new_hash = security.new_refresh_token()
        # 원자 CAS: 미폐기 행만 폐기 처리 — 영향 0행이면 재사용 의심
        result = await session.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == token_hash,
                   RefreshToken.revoked_at.is_(None),
                   RefreshToken.expires_at > now)
            .values(revoked_at=now, replaced_by=new_hash)
            .returning(RefreshToken.user_id, RefreshToken.family_id)
        )
        row = result.first()

        if row is None:
            # 이미 회전됐거나 만료 — 재사용 판정 (grace window 내 직전 회전은 무시)
            prev = (await session.execute(
                select(RefreshToken).where(RefreshToken.token_hash == token_hash)
            )).scalar_one_or_none()
            if prev is None:
                raise TokenError()
            grace = timedelta(seconds=s.refresh_grace_seconds)
            revoked_at = prev.revoked_at
            if revoked_at is not None and revoked_at.tzinfo is None:
                revoked_at = revoked_at.replace(tzinfo=timezone.utc)  # sqlite는 tz 미보존
            if revoked_at and (now - revoked_at) <= grace:
                # 동시 탭/재시도 오탐 — 이번 요청은 거부하되 family는 유지
                raise TokenError("토큰이 방금 회전되었습니다 — 재시도하세요")
            # grace 밖 재사용 = 탈취 의심 → family 전체 폐기 + 사용자 전 세션 무효화
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == prev.family_id,
                       RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await self.bump_token_version(session, user_id=prev.user_id)
            raise TokenError("토큰 재사용이 감지되어 모든 세션이 종료되었습니다")

        user_id, family_id = row
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        if not user.is_active:
            # 비활성(퇴사 차단·관리자 비활성) 계정은 갱신 불가 — 종전엔 is_active를 보지 않아
            # 남아 있던 refresh 쿠키로 7일간 세션이 계속 재발급됐다(v1.288 흐름검토 G1).
            # ⚠ 아래 폐기는 예외로 롤백된다(get_session) — 차단을 보장하는 것은 매 요청의 이 is_active
            #   검사이고, refresh 행 전량 폐기는 비활성화 시점(deactivate)에 이미 커밋돼 있다.
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now))
            raise TokenError("비활성 계정입니다")
        return await self._issue_tokens(session, user, family_id=family_id,
                                        new_pair=(new_raw, new_hash))

    async def deactivate(self, session: AsyncSession, *, user: User) -> None:
        """계정 비활성 — 모든 refresh 폐기 + tv 증가 + Redis tv 캐시 삭제(발급 토큰 즉시 무효).

        is_active만 끄고 tv만 올리면 ① refresh로 재발급되고 ② 캐시된 tv(1시간)로 기존 액세스
        토큰이 만료까지 통한다 — 차단의 단일 경로로 이 함수를 쓴다.
        """
        user.is_active = False
        await session.flush()
        await self.logout(session, user_id=user.id)

    # ── 강제 무효화 (design §6.1 Redis write-through) ───────
    async def bump_token_version(self, session: AsyncSession, *, user_id: int) -> None:
        await session.execute(
            update(User).where(User.id == user_id)
            .values(token_version=User.token_version + 1)
        )
        if self._redis is not None:
            try:
                await self._redis.delete(TV_CACHE_KEY.format(user_id=user_id))
            except Exception:  # noqa: BLE001 — 캐시 삭제 실패가 차단 자체를 막지 않는다
                import logging
                logging.getLogger("erp.auth").warning(
                    "tv 캐시 삭제 실패 — 기존 액세스 토큰이 캐시 만료(최대 1시간)까지 유효할 수 있음",
                    extra={"ctx": {"user_id": user_id}})

    async def current_token_version(self, session: AsyncSession, *, user_id: int) -> int:
        """미들웨어용 — Redis 우선, 미스 시 DB 폴백 후 캐시 (design §6.1)."""
        key = TV_CACHE_KEY.format(user_id=user_id)
        if self._redis is not None:
            cached = await self._redis.get(key)
            if cached is not None:
                return int(cached)
        tv = (await session.execute(
            select(User.token_version).where(User.id == user_id)
        )).scalar_one_or_none()
        if tv is None:
            raise TokenError()
        if self._redis is not None:
            await self._redis.set(key, tv, ex=3600)
        return tv

    async def logout(self, session: AsyncSession, *, user_id: int) -> None:
        now = datetime.now(timezone.utc)
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await self.bump_token_version(session, user_id=user_id)

    # ── 내부 ────────────────────────────────────────────────
    async def _issue_tokens(self, session: AsyncSession, user: User, *, family_id: str,
                            new_pair: tuple[str, str] | None = None) -> dict:
        s = get_settings()
        raw, token_hash = new_pair or security.new_refresh_token()
        session.add(RefreshToken(
            user_id=user.id, token_hash=token_hash, family_id=family_id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=s.refresh_token_days),
        ))
        access = security.create_access_token(
            user_id=user.id, role=user.role, dept_id=user.department_id,
            token_version=user.token_version, license_flag=user.license_flag_active,
        )
        return {"access_token": access, "refresh_token": raw, "token_type": "bearer"}
