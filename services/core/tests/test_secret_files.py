"""권한 파일 기반 시크릿 로딩 (P2-5) — *_FILE 우선·부재 시 env 폴백(무회귀)."""
from erp.config import Settings


def test_secret_file_overrides_env(tmp_path):
    """*_FILE이 존재하면 파일 값이 env 값을 덮어쓴다(docker inspect 노출 회피)."""
    f = tmp_path / "data_enc_key"
    f.write_text("ab" * 32 + "\n")   # 개행 포함 → strip 확인
    s = Settings(env="dev", data_enc_key="cd" * 32, data_enc_key_file=str(f))
    s.load_secret_files()
    assert s.data_enc_key == "ab" * 32


def test_secret_file_absent_keeps_env(tmp_path):
    """*_FILE 미설정/부재 시 env 값 유지(폴백·무회귀)."""
    s = Settings(env="dev", data_enc_key="cd" * 32,
                 data_enc_key_file=str(tmp_path / "nope"))
    s.load_secret_files()
    assert s.data_enc_key == "cd" * 32


def test_phone_bidx_and_v2_from_file(tmp_path):
    bidx = tmp_path / "bidx"
    bidx.write_text("ef" * 32)
    v2 = tmp_path / "v2"
    v2.write_text("12" * 32)
    s = Settings(env="dev", phone_bidx_key_file=str(bidx), data_enc_key_v2_file=str(v2))
    s.load_secret_files()
    assert s.phone_bidx_key == "ef" * 32 and s.data_enc_key_v2 == "12" * 32


def test_prod_guard_blocks_placeholder_phone_bidx():
    """PHONE_BIDX_KEY 공백(공개 상수) 시 prod fail-closed — blind index 전화번호 열거 방지(적대검토)."""
    import pytest

    # prod + phone_bidx 미주입("1"*64) → 기동 차단
    s = Settings(env="prod", jwt_secret="x" * 40, data_enc_key="ab" * 32,
                 phone_bidx_key="1" * 64)
    with pytest.raises(RuntimeError, match="PHONE_BIDX_KEY"):
        s.load_secret_files()
    # 실키 주입 시 통과
    ok = Settings(env="prod", jwt_secret="x" * 40, data_enc_key="ab" * 32,
                  phone_bidx_key="ab" * 32)
    ok.load_secret_files()   # 예외 없음
    assert ok.phone_bidx_key == "ab" * 32


def test_client_ip_uses_rightmost_xff():
    """스로틀 키 IP는 신뢰 프록시가 덧붙인 XFF 오른쪽 값 — 스푸핑된 왼쪽 값 무시(적대검토 MED)."""
    from erp.scm.router import _client_ip

    class _Req:
        def __init__(self, xff, client_host="10.0.0.9"):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": client_host})()

    # 공격자 스푸핑(왼쪽) + nginx 실제IP(오른쪽) → 오른쪽 채택
    assert _client_ip(_Req("1.2.3.4, 203.0.113.7")) == "203.0.113.7"
    # XFF 없음 → client.host
    assert _client_ip(_Req("")) == "10.0.0.9"


def test_dummy_verify_runs_for_missing_user():
    """미존재 사용자 argon2 타이밍 균일화 — dummy_verify가 예외 없이 수행(열거 방지)."""
    from erp.common import security

    security.dummy_verify("anything")   # 예외 없이 통과(결과 폐기)
