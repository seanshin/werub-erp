"""컬럼 암호화 — GCM 포맷·AAD 바인딩·blind index (design §6.4)."""
import pytest

from erp.common import crypto


def test_roundtrip():
    aad = crypto.column_aad("hr.employees.phone", 42)
    blob = crypto.encrypt_column("010-1234-5678", aad=aad)
    assert crypto.decrypt_column(blob, aad=aad) == "010-1234-5678"


def test_format_layout():
    """key_version(1B) ‖ nonce(12B) ‖ ct+tag — design §6.4 레이아웃."""
    blob = crypto.encrypt_column("x", aad="t:1")
    assert blob[0] == 1                      # key_version
    assert len(blob) == 1 + 12 + 1 + 16      # ver + nonce + ct(1) + tag(16)


def test_nonce_never_reused():
    """동일 평문도 매번 다른 암호문 (nonce 신규 생성)."""
    aad = "t:1"
    assert crypto.encrypt_column("same", aad=aad) != crypto.encrypt_column("same", aad=aad)


def test_aad_binding_detects_row_swap():
    """행 간 암호문 치환 공격 — AAD 불일치로 복호 실패."""
    blob = crypto.encrypt_column("010-1234-5678",
                                 aad=crypto.column_aad("hr.employees.phone", 1))
    with pytest.raises(Exception):
        crypto.decrypt_column(blob, aad=crypto.column_aad("hr.employees.phone", 2))


def test_tamper_detected():
    aad = "t:1"
    blob = bytearray(crypto.encrypt_column("data", aad=aad))
    blob[-1] ^= 0xFF  # tag 변조
    with pytest.raises(Exception):
        crypto.decrypt_column(bytes(blob), aad=aad)


def test_blind_index_normalizes():
    """전화번호 정규화 — 형식 달라도 동일 인덱스 (전체 일치 검색)."""
    assert crypto.phone_blind_index("010-1234-5678") == crypto.phone_blind_index("01012345678")
    assert crypto.phone_blind_index("010-1234-5678") != crypto.phone_blind_index("010-1234-5679")


def test_blind_index_key_separate_from_enc_key():
    """bidx 키는 DATA_ENC_KEY와 분리 — 키 동일 입력에도 다른 결과 보장 확인."""
    from erp.config import get_settings
    s = get_settings()
    assert s.data_enc_key != s.phone_bidx_key


def test_mask_phone():
    assert crypto.mask_phone("010-1234-5678") == "010-****-5678"


def _set_v2(monkeypatch):
    """v2 키 주입 + 현재 버전 승격 (회전 단계 2 재현)."""
    from erp.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "data_enc_key_v2", "a" * 64, raising=False)
    monkeypatch.setattr(s, "data_enc_key_version", 2, raising=False)


def test_rotation_old_blob_still_decrypts(monkeypatch):
    """회전 중(v1→v2) 구 v1 blob은 구 키로 계속 복호 — 무중단(P2-6)."""
    aad = crypto.column_aad("hr.employees.phone", 7)
    v1_blob = crypto.encrypt_column("010-1234-5678", aad=aad)   # 현재 v1
    assert crypto.key_version_of(v1_blob) == 1
    _set_v2(monkeypatch)
    # 현재 버전은 v2지만, v1 키 유지되어 구 blob 복호 가능
    assert crypto.decrypt_column(v1_blob, aad=aad) == "010-1234-5678"
    # 신규 암호화는 v2로 부킹
    v2_blob = crypto.encrypt_column("new", aad=aad)
    assert crypto.key_version_of(v2_blob) == 2


def test_reencrypt_stale_to_current(monkeypatch):
    """reencrypt_column — v1 blob을 v2로 재암호화(단계 3). 값 보존·버전 승격·멱등."""
    aad = crypto.column_aad("scm.suppliers.tax_id", 3)
    v1_blob = crypto.encrypt_column("123-45-67890", aad=aad)
    _set_v2(monkeypatch)
    new_blob = crypto.reencrypt_column(v1_blob, aad=aad)
    assert new_blob is not None
    assert crypto.key_version_of(new_blob) == 2
    assert crypto.decrypt_column(new_blob, aad=aad) == "123-45-67890"
    # 이미 현재 버전이면 None(멱등 — 재실행 안전)
    assert crypto.reencrypt_column(new_blob, aad=aad) is None
