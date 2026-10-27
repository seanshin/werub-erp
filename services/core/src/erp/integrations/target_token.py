"""S-3 대상 토큰(ERP→HIS 발신) — X-Target-Token 생성 (his-erp-integration-auth-hardening §S-3).

유출 PII키 단독의 임의 대상 순회 차단: 호출 대상(사번/mrn/drafter)별 단기 HMAC 토큰을 동봉한다.
형식: <target>.<expEpochSec>.<HMAC-SHA256("<target>.<exp>", secret) hex>. exp ≤300초 권장.

대상 EP(HIS 배선 완료): GET /integration/hr/staff/:emp/schedule · GET /integration/patients/:mrn
· GET /erp/eapproval/approvers?drafter_clinic_user_id=. ERP는 현재 이 EP들의 상시 호출부가 없으나,
신규 호출부는 반드시 본 헬퍼로 토큰을 동봉할 것(HIS enforce 플립 후 미동봉=401).
"""
import hashlib
import hmac
import time

from erp.config import get_settings

DEFAULT_TTL_SEC = 300


def make_target_token(target: str, *, ttl_sec: int = DEFAULT_TTL_SEC,
                      secret: str | None = None, now: int | None = None) -> str:
    """대상 토큰 생성 — secret 미주입 시 config(his_target_token_secret) 사용."""
    sec = secret if secret is not None else get_settings().his_target_token_secret
    if not sec:
        raise ValueError("his_target_token_secret 미설정 — S-3 대상 토큰을 만들 수 없습니다")
    if not target:
        raise ValueError("target 필수")
    exp = int(now if now is not None else time.time()) + int(ttl_sec)
    msg = f"{target}.{exp}"
    mac = hmac.new(sec.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{msg}.{mac}"


def target_token_header(target: str, **kw) -> dict:
    """호출부 편의 — {"X-Target-Token": ...} 헤더 dict."""
    return {"X-Target-Token": make_target_token(target, **kw)}
