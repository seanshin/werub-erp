"""컬럼 레벨 암호화 — design §6.4 GCM 저장 포맷·운영 규칙.

레이아웃: key_version(1B) ‖ nonce(12B, os.urandom — 재사용 절대 금지) ‖ ciphertext ‖ tag(16B)
AAD = "{schema.table.column}:{row_pk}" — 행 간/컬럼 간 암호문 치환 공격 탐지
blind index = HMAC-SHA256(PHONE_BIDX_KEY, 정규화 전화번호) — 전체 일치 검색 전용
"""
import hashlib
import hmac
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from erp.config import get_settings

def _keys() -> dict[int, bytes]:
    """버전→키 매핑 — 회전 중엔 구·신 키가 공존(구 blob 복호 + 신 blob 암호화, P2-6)."""
    s = get_settings()
    keys = {1: bytes.fromhex(s.data_enc_key)}
    if s.data_enc_key_v2:
        keys[2] = bytes.fromhex(s.data_enc_key_v2)
    return keys


def current_key_version() -> int:
    """신규 암호화에 쓸 현재 키 버전(설정값)."""
    return get_settings().data_enc_key_version


def _key(version: int) -> bytes:
    keys = _keys()
    if version not in keys:
        raise ValueError(f"미지원/미설정 key_version: {version} (키 회전 시 구 키 유지 필요)")
    return keys[version]


def encrypt_column(plaintext: str, *, aad: str) -> bytes:
    """암호화 — nonce는 매 호출 신규 생성 (카운터·파생 금지). 현재 키 버전으로 부킹."""
    version = current_key_version()
    nonce = os.urandom(12)
    ct = AESGCM(_key(version)).encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
    return bytes([version]) + nonce + ct  # ct에 tag(16B) 포함


def decrypt_column(blob: bytes, *, aad: str) -> str:
    """복호 — blob의 버전 바이트로 키 선택. AAD 불일치(치환 공격)·위변조 시 InvalidTag 예외."""
    version, nonce, ct = blob[0], blob[1:13], blob[13:]
    return AESGCM(_key(version)).decrypt(nonce, ct, aad.encode("utf-8")).decode("utf-8")


def key_version_of(blob: bytes) -> int:
    """blob의 키 버전 바이트 — 회전 진척 스캔용."""
    return blob[0]


def reencrypt_column(blob: bytes, *, aad: str) -> bytes | None:
    """스테일 버전 blob을 현재 버전 키로 재암호화. 이미 현재 버전이면 None(변경 불요·P2-6).

    복호는 blob에 박힌 구 버전 키로, 재암호화는 현재 버전 키로 — 동일 AAD 유지(행 바인딩 보존)."""
    if blob[0] == current_key_version():
        return None
    return encrypt_column(decrypt_column(blob, aad=aad), aad=aad)


def column_aad(table_column: str, row_pk: str | int) -> str:
    return f"{table_column}:{row_pk}"


_NON_DIGIT = re.compile(r"\D")


def phone_blind_index(phone: str) -> str:
    """전화번호 blind index — 정규화(숫자만) 후 HMAC. 전체 일치 검색 전용 (design §6.4)."""
    normalized = _NON_DIGIT.sub("", phone)
    key = bytes.fromhex(get_settings().phone_bidx_key)
    return hmac.new(key, normalized.encode(), hashlib.sha256).hexdigest()


def mask_email(email: str) -> str:
    """이메일 로그 마스킹 — 로컬부 1자만 노출(PII 로그 노출 방지)."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def mask_id(value: str) -> str:
    """식별자(사번 등) 로그 마스킹 — 앞 2자만."""
    return (value[:2] + "***") if value and len(value) > 2 else "***"


# 감사로그 before/after 마스킹 — 키 이름 + 값 모양 **양쪽**으로 판정한다.
# 키 이름만 보면 앞으로 생길 키를 놓치고, 값만 보면 짧은 계좌·이름을 놓친다.
_SENSITIVE_KEY_PARTS = (
    "account_no", "accountno", "iban", "swift",          # 계좌 — 화면은 마스킹하는데 감사로그로 새면 무의미
    "ssn", "rrn", "resident_no", "jumin",                 # 주민등록번호
    "base_pay", "salary", "pay_amount", "annual_pay",     # 급여
    "dependent",                                          # 부양가족 상세
    "biz_reg_no", "card_no", "cardno",
    "password", "secret", "token", "api_key", "webhook_key", "pin_hash",
    # 환자 식별자 — Phase0 규율(로그·화면 노출 최소화). 현재 감사 스냅샷에 이 키가 실린
    # 도메인은 없지만, 새 도메인이 실으면 값 모양 판정으로는 못 잡는다(`P2026-0005`는
    # 자릿수 패턴에 걸리지 않는다) — 키 이름 쪽에서 미리 막는다.
    "chart_no", "chartno", "patient_name", "patient_id", "patientref", "patient_ref",
)
_RRN = re.compile(r"^\d{6}[-\s]?\d{7}$")               # 주민등록번호 모양(값 전체)
# 자유서술(note·reason 등) **안에 박힌** 주민번호 — 실제로 이런 필드에서 샌다.
# 계좌·주문번호처럼 자릿수만으로는 구분 안 되는 것은 부분치환하지 않는다(과잉 마스킹 방지).
_RRN_INLINE = re.compile(r"\b\d{6}[-\s]\d{7}\b")
_LONG_DIGITS = re.compile(r"^[\d-]{10,}$")               # 계좌·카드 모양(구분자 포함 10자 이상)
_MASK = "***"


def _looks_sensitive_value(v: str) -> bool:
    t = v.replace(" ", "")
    return bool(_RRN.match(t) or (_LONG_DIGITS.match(t) and sum(c.isdigit() for c in t) >= 10))


def mask_audit_payload(value, _depth: int = 0):
    """감사로그 before/after의 민감값 마스킹 — 관리자 화면 노출용.

    감사로그는 도메인마다 다른 스냅샷을 담아 **어떤 키가 올지 미리 알 수 없다.** 그래서
    키 이름 부분일치(알려진 민감 키)와 값 모양(주민번호·계좌 자릿수) 두 경로로 판정한다.
    한쪽만 쓰면: 키 이름만 → 새 키를 놓치고, 값만 → 짧은 계좌·이름을 놓친다.
    `admin.audit read`는 DIRECTOR/ADMIN뿐이지만, 화면에서 마스킹하는 값을 감사로그 경유로
    평문 노출하면 그 마스킹이 무의미해진다(계좌 마스킹 v1.159와 같은 취지).
    """
    if _depth > 6:                     # 병리적 중첩 방어 — 깊은 곳은 통째로 가린다
        return _MASK
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            if any(part in kl for part in _SENSITIVE_KEY_PARTS):
                out[k] = f"[{len(v)}건]" if isinstance(v, list) else _MASK
            else:
                out[k] = mask_audit_payload(v, _depth + 1)
        return out
    if isinstance(value, list):
        return [mask_audit_payload(v, _depth + 1) for v in value]
    if isinstance(value, str):
        if _looks_sensitive_value(value):
            return _MASK
        return _RRN_INLINE.sub(_MASK, value)
    return value


def mask_chart_no(chart_no: str | None) -> str | None:
    """차트번호 표시 마스킹 — 환자 식별 권한이 없는 화면(조달·물류 등)용.

    행 구분(어느 사용분이 어느 시술 건인지)은 남기되 환자 특정은 막는다. 원문은 DB·감사로그에
    그대로 남으므로 대사는 권한자가 수행한다(Phase0 규율: 환자식별자는 필요 최소 범위에만)."""
    if not chart_no:
        return chart_no
    return f"{chart_no[:2]}***{chart_no[-2:]}" if len(chart_no) > 6 else "***"


def mask_phone(phone: str) -> str:
    """표시 기본값 — 권한자만 복호 원문 (NFR 마스킹)."""
    digits = _NON_DIGIT.sub("", phone)
    if len(digits) < 7:
        return "***"
    return f"{digits[:3]}-****-{digits[-4:]}"
