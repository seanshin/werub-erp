"""PIN + WebAuthn(패스키) 2FA — PIN 전체·옵션 생성·오케스트레이션(verify 모킹)."""
import pytest
from webauthn.helpers import bytes_to_base64url

from erp.common.auth2fa_service import TwoFactorService
from erp.common.models import User, UserCredential
from erp.errors import TokenError, ValidationError

svc = TwoFactorService()
CID = bytes_to_base64url(b"cred-1")     # 유효 base64url (실코드도 동일 형식)
PUB = bytes_to_base64url(b"pubkey")


async def _user(session, email="u@x.example"):
    u = User(email=email, password_hash="x", role="STAFF")
    session.add(u)
    await session.flush()
    return u


async def test_set_and_verify_pin(session):
    u = await _user(session)
    await svc.set_pin(session, user_id=u.id, pin="1234")
    assert svc.verify_pin(u, "1234") is True
    assert svc.verify_pin(u, "0000") is False


async def test_pin_format_rejected(session):
    u = await _user(session, "p@x.example")
    for bad in ("12", "abcd", "123456789", ""):
        with pytest.raises(ValidationError):
            await svc.set_pin(session, user_id=u.id, pin=bad)


async def test_register_begin_options_and_token(session):
    u = await _user(session, "r@x.example")
    out = await svc.register_begin(session, user=u)
    assert "challenge" in out["options"] and out["options"]["rp"]["id"]
    d = svc._verify(out["token"], "reg")          # 토큰 purpose 검증
    assert d["sub"] == str(u.id) and d["chal"]
    with pytest.raises(TokenError):               # 잘못된 purpose 거부
        svc._verify(out["token"], "2fa")


async def test_register_complete_stores_credential(session, monkeypatch):
    u = await _user(session, "rc@x.example")
    begin = await svc.register_begin(session, user=u)

    class _Ver:
        credential_id = b"\x01\x02\x03"
        credential_public_key = b"\x09\x08\x07"
        sign_count = 0
    monkeypatch.setattr("erp.common.auth2fa_service.verify_registration_response",
                        lambda **kw: _Ver())
    cred = await svc.register_complete(session, user=u, token=begin["token"],
                                       credential={"id": "x"}, name="내폰")
    assert cred.name == "내폰" and cred.sign_count == 0
    rows = await svc._creds(session, u.id)
    assert len(rows) == 1


async def test_auth_begin_requires_credential(session):
    u = await _user(session, "ab@x.example")
    with pytest.raises(ValidationError, match="패스키"):     # 등록 전
        await svc.auth_begin(session, user=u)


async def test_auth_complete_updates_sign_count(session, monkeypatch):
    u = await _user(session, "ac@x.example")
    session.add(UserCredential(user_id=u.id, credential_id=CID,
                               public_key=PUB, sign_count=5))
    await session.flush()
    begin = await svc.auth_begin(session, user=u)

    class _Ver:
        new_sign_count = 6
    monkeypatch.setattr("erp.common.auth2fa_service.verify_authentication_response",
                        lambda **kw: _Ver())
    user = await svc.auth_complete(session, token=begin["token"],
                                   assertion={"id": CID})
    assert user.id == u.id
    cred = (await svc._creds(session, u.id))[0]
    assert cred.sign_count == 6 and cred.last_used_at is not None


async def test_auth_complete_unknown_credential(session):
    u = await _user(session, "uc@x.example")
    session.add(UserCredential(user_id=u.id, credential_id=CID, public_key=PUB))
    await session.flush()
    begin = await svc.auth_begin(session, user=u)
    with pytest.raises(ValidationError, match="등록되지 않은"):
        await svc.auth_complete(session, token=begin["token"],
                                assertion={"id": bytes_to_base64url(b"nope")})


async def test_auth_complete_challenge_single_use(session, monkeypatch):
    """WebAuthn challenge 1회 소비 — 캡처된 assertion 재전송이 재생으로 거부(적대검토 MED)."""
    class _FakeRedis:
        def __init__(self):
            self.keys = {}

        async def set(self, k, v, nx=False, ex=None):
            if nx and k in self.keys:
                return None            # 이미 소비 → NX 실패
            self.keys[k] = v
            return True

    fake = _FakeRedis()
    monkeypatch.setattr("erp.common.redis_client.get_redis", lambda: fake)

    u = await _user(session, "replay@x.example")
    session.add(UserCredential(user_id=u.id, credential_id=CID, public_key=PUB, sign_count=0))
    await session.flush()
    begin = await svc.auth_begin(session, user=u)

    class _Ver:
        new_sign_count = 0             # 플랫폼 패스키(sign_count 0 고정) — 카운터 방어 무효 상황
    monkeypatch.setattr("erp.common.auth2fa_service.verify_authentication_response",
                        lambda **kw: _Ver())
    # 1회차 성공
    user = await svc.auth_complete(session, token=begin["token"], assertion={"id": CID})
    assert user.id == u.id
    # 동일 token(=동일 challenge) 재전송 → 재생 거부
    with pytest.raises(TokenError):
        await svc.auth_complete(session, token=begin["token"], assertion={"id": CID})
