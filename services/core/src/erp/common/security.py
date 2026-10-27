"""비밀번호 해싱·JWT — design §6.1."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from erp.config import get_settings
from erp.errors import TokenError

_ph = PasswordHasher()  # argon2id 기본

MIN_PASSWORD_LEN = 10


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except VerifyMismatchError:
        return False


# 존재하지 않는 계정 로그인 시에도 argon2 검증 시간을 균일화(사용자 열거 타이밍 오라클 방지·적대검토 LOW).
# 미존재 사용자면 verify_password를 건너뛰어 응답이 빨라져 계정 존재 여부가 누설되던 갭.
_DUMMY_HASH = _ph.hash("timing-equalizer-not-a-real-password")


def dummy_verify(plain: str) -> None:
    """미존재 사용자 경로에서 호출 — 실제 검증과 동등한 argon2 시간을 소비(결과 폐기)."""
    try:
        _ph.verify(_DUMMY_HASH, plain)
    except VerifyMismatchError:
        pass


def validate_password_policy(plain: str) -> bool:
    """최소 10자 + 영문/숫자/특수 중 2종 이상 (design §6.1)."""
    if len(plain) < MIN_PASSWORD_LEN:
        return False
    kinds = sum([any(c.isalpha() for c in plain),
                 any(c.isdigit() for c in plain),
                 any(not c.isalnum() for c in plain)])
    return kinds >= 2


def create_access_token(*, user_id: int, role: str, dept_id: int | None,
                        token_version: int, license_flag: bool) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id), "role": role, "dept": dept_id,
        "tv": token_version, "lic": license_flag,
        "iat": now, "exp": now + timedelta(minutes=s.access_token_minutes),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    try:
        # 필수 클레임 강제 (보안 H-4) — exp 누락 토큰 위조 차단. alg는 HS256 고정.
        return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"],
                          options={"require": ["exp", "iat", "sub", "tv"]})
    except jwt.ExpiredSignatureError:
        raise TokenError("토큰이 만료되었습니다")
    except jwt.InvalidTokenError:
        raise TokenError()


def new_refresh_token() -> tuple[str, str]:
    """(원문, SHA-256 hash) — 원문은 클라이언트에만, DB엔 hash만 (design §6.4)."""
    raw = secrets.token_urlsafe(48)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
