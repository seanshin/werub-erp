"""아웃바운드 허용 호스트 — 연동 대상 시스템별 단일 계산점(SSRF 화이트리스트).

종전엔 모듈마다 운영 도메인 문자열을 코드에 박아 두어(his_billing·esign_emp_map·his_fee_push·
settlement_callback·his_sign_origination·eapproval·groupware·clinic_staff·weve_rag·ai_publisher),
다른 기관·다른 호스트에 설치하면 **설정이 아니라 코드를 고쳐야** 호출이 나갔다. 허용 집합 = 기존 기본값 ∪ 명시 목록(`HIS_ALLOWED_HOSTS` 등 CSV).

⚠ base URL의 호스트를 **자동으로 허용하지 않는다**(INV-NET-01). base가 오설정·오염돼도 직원 보수·
환자 청구 페이로드가 임의 호스트로 나가지 않게 하는 것이 이 목록의 목적이다 — 주소와 허용 목록을
따로 적게 해 한 곳의 실수가 곧 유출이 되지 않게 한다. 본문 유래 URL(서명 완료본 등)도 같은 목록.
"""
from urllib.parse import urlparse

from erp.config import get_settings

_LOOPBACK = frozenset({"localhost", "127.0.0.1"})

# 시스템 → (기본 호스트, 명시 목록 설정 이름)
_SYSTEMS: dict[str, tuple[frozenset[str], str]] = {
    "his": (frozenset({"his.example.org"}), "his_allowed_hosts"),
    "clinic": (frozenset({"clinic.example.org"}), "clinic_allowed_hosts"),
    "ai": (frozenset({"example.org"}), "ai_allowed_hosts"),
}


def _csv(v: str | None) -> set[str]:
    return {h.strip().lower() for h in (v or "").split(",") if h.strip()}


def allowed_hosts(system: str, *, loopback: bool = False) -> frozenset[str]:
    """`system`(his|clinic|ai) 아웃바운드 허용 호스트.

    loopback=True면 localhost·127.0.0.1을 더한다 — 종전에 루프백을 허용하던 호출부의 동작 보존용
    (동일 호스트 중계 배치). 본문 유래 URL을 검사하는 곳은 넘기지 말 것.
    """
    defaults, extra_attr = _SYSTEMS[system]
    hosts = set(defaults) | _csv(getattr(get_settings(), extra_attr, None))
    if loopback:
        hosts |= _LOOPBACK
    return frozenset(hosts)


def check_host(system: str, url: str, *, loopback: bool = False) -> str:
    """url의 호스트가 허용 집합이면 호스트를, 아니면 ValueError(사유에 설정 이름을 담는다)."""
    host = (urlparse(url).hostname or "").lower()
    if host not in allowed_hosts(system, loopback=loopback):
        attr = _SYSTEMS[system][1].upper()
        raise ValueError(f"허용되지 않은 대상: {host or url} — 정상 대상이면 서버 설정 "
                         f"{attr}(CSV)에 추가하세요")
    return host
