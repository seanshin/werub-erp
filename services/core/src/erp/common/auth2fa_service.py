"""PIN + WebAuthn(패스키) 2FA 로그인 — HIS SSO와 별개 수단.

상태비저장: WebAuthn challenge를 서명 토큰(JWT, 단기)에 담아 begin→complete 왕복(Redis 비의존).
PIN(지식) 통과 후에만 패스키(소유·생체) 토큰 발급 → complete가 패스키 검증 → 세션 발급.
실제 생체 ceremony는 브라우저/실기기에서 수행, 서버는 등록·검증 로직만.
"""
import json
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options, generate_registration_options, options_to_json,
    verify_authentication_response, verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor, ResidentKeyRequirement,
    UserVerificationRequirement,
)

from erp.common import security
from erp.common.models import User, UserCredential
from erp.config import get_settings
from erp.errors import TokenError, ValidationError


class TwoFactorService:
    # ── PIN ───────────────────────────────────────────────
    @staticmethod
    def _check_pin_format(pin: str) -> None:
        if not (pin and pin.isdigit() and 4 <= len(pin) <= 8):
            raise ValidationError("PIN은 숫자 4~8자리여야 합니다")

    async def set_pin(self, session: AsyncSession, *, user_id: int, pin: str) -> None:
        self._check_pin_format(pin)
        user = await session.get(User, user_id)
        if user is None:
            raise ValidationError("존재하지 않는 사용자입니다")
        user.pin_hash = security.hash_password(pin)

    @staticmethod
    def verify_pin(user: User, pin: str) -> bool:
        return bool(user.pin_hash) and security.verify_password(pin, user.pin_hash)

    # ── 단기 서명 토큰(challenge 운반) ─────────────────────
    @staticmethod
    def _sign(purpose: str, claims: dict, ttl: int = 300) -> str:
        now = datetime.now(timezone.utc)
        return jwt.encode({**claims, "purpose": purpose, "iat": now,
                           "exp": now + timedelta(seconds=ttl)},
                          get_settings().jwt_secret, algorithm="HS256")

    @staticmethod
    def _verify(token: str, purpose: str) -> dict:
        try:
            d = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"],
                           options={"require": ["exp", "purpose"]})
        except jwt.PyJWTError as exc:
            raise TokenError() from exc
        if d.get("purpose") != purpose:
            raise TokenError()
        return d

    @staticmethod
    async def _consume_challenge(purpose: str, chal: str) -> None:
        """challenge 1회 소비(재생 방지) — Redis NX. 이미 소비됐으면 TokenError(재생).

        상태비저장 JWT challenge는 TTL(300s) 내 재전송 가능하고, 플랫폼 패스키는 sign_count=0
        고정이라 카운터 방어도 무효 → SSO jti(sso_service) 동형 1회 소비로 재생 창 차단(적대검토 MED).
        Redis 장애 시 가용성 우선(생략)·TTL+서명이 상한.
        """
        import hashlib

        from erp.common.redis_client import get_redis
        digest = hashlib.sha256(f"{purpose}:{chal}".encode()).hexdigest()
        try:
            r = get_redis()
            ok = await r.set(f"webauthn:chal:{digest}", "1", nx=True, ex=300)
            if not ok:
                raise TokenError()
        except TokenError:
            raise
        except Exception:  # noqa: BLE001 — redis 장애 시 가용성 우선(TTL·서명이 상한)
            pass

    async def _creds(self, session: AsyncSession, user_id: int) -> list[UserCredential]:
        return list((await session.execute(
            select(UserCredential).where(UserCredential.user_id == user_id))).scalars())

    # ── 패스키 등록 ───────────────────────────────────────
    async def register_begin(self, session: AsyncSession, *, user: User) -> dict:
        s = get_settings()
        existing = await self._creds(session, user.id)
        opts = generate_registration_options(
            rp_id=s.webauthn_rp_id, rp_name=s.webauthn_rp_name,
            user_id=str(user.id).encode(), user_name=user.email, user_display_name=user.email,
            exclude_credentials=[PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(c.credential_id)) for c in existing],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED))
        token = self._sign("reg", {"sub": str(user.id),
                                   "chal": bytes_to_base64url(opts.challenge)})
        return {"options": json.loads(options_to_json(opts)), "token": token}

    async def register_complete(self, session: AsyncSession, *, user: User, token: str,
                                credential: dict, name: str | None = None) -> UserCredential:
        s = get_settings()
        d = self._verify(token, "reg")
        if d.get("sub") != str(user.id):
            raise TokenError()
        await self._consume_challenge("reg", d["chal"])   # 1회 소비(재생 방지)
        ver = verify_registration_response(
            credential=credential, expected_challenge=base64url_to_bytes(d["chal"]),
            expected_rp_id=s.webauthn_rp_id, expected_origin=s.webauthn_origin)
        cred = UserCredential(
            user_id=user.id, credential_id=bytes_to_base64url(ver.credential_id),
            public_key=bytes_to_base64url(ver.credential_public_key),
            sign_count=ver.sign_count, name=name)
        session.add(cred)
        await session.flush()
        return cred

    # ── 패스키 인증(로그인 2단계) ─────────────────────────
    async def auth_begin(self, session: AsyncSession, *, user: User) -> dict:
        s = get_settings()
        creds = await self._creds(session, user.id)
        if not creds:
            raise ValidationError("등록된 패스키가 없습니다 (먼저 생체인증 등록)")
        opts = generate_authentication_options(
            rp_id=s.webauthn_rp_id,
            allow_credentials=[PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(c.credential_id)) for c in creds],
            user_verification=UserVerificationRequirement.REQUIRED)
        token = self._sign("2fa", {"sub": str(user.id),
                                   "chal": bytes_to_base64url(opts.challenge)})
        return {"options": json.loads(options_to_json(opts)), "token": token}

    async def auth_complete(self, session: AsyncSession, *, token: str,
                            assertion: dict) -> User:
        s = get_settings()
        d = self._verify(token, "2fa")
        await self._consume_challenge("2fa", d["chal"])   # 1회 소비(재생 방지)
        user_id = int(d["sub"])
        cred = (await session.execute(
            select(UserCredential).where(UserCredential.user_id == user_id,
                                         UserCredential.credential_id == assertion.get("id")))
        ).scalar_one_or_none()
        if cred is None:
            raise ValidationError("등록되지 않은 패스키입니다")
        ver = verify_authentication_response(
            credential=assertion, expected_challenge=base64url_to_bytes(d["chal"]),
            expected_rp_id=s.webauthn_rp_id, expected_origin=s.webauthn_origin,
            credential_public_key=base64url_to_bytes(cred.public_key),
            credential_current_sign_count=cred.sign_count, require_user_verification=True)
        cred.sign_count = ver.new_sign_count
        cred.last_used_at = datetime.now(timezone.utc)
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            raise ValidationError("비활성 사용자입니다")
        return user
