"""계약 생애주기 공통 커널 — 위수탁(scm)·의료(hr) 계약이 공유하는 상태머신.

순수 커널(DB·도메인 의존 없음). 상태 상수·전이 가드·발생가능 상태만 정의하고,
각 도메인 서비스(scm.contract_service·hr.contract_service)가 import해 일원화.
"""
from erp.errors import ValidationError

DRAFT = "DRAFT"
PENDING_APPROVAL = "PENDING_APPROVAL"
APPROVED = "APPROVED"
ACTIVE = "ACTIVE"
EXPIRING = "EXPIRING"
EXPIRED = "EXPIRED"
TERMINATED = "TERMINATED"
REJECTED = "REJECTED"

STATUSES = (DRAFT, PENDING_APPROVAL, APPROVED, ACTIVE, EXPIRING, EXPIRED, TERMINATED, REJECTED)
STATUS_SQL = "'" + "','".join(STATUSES) + "'"   # CHECK (status IN (...)) 절 재사용

# 생애주기 전이 가드 — 불법 전이 차단. 기존 종단값(ACTIVE/EXPIRED/TERMINATED) 무손실.
TRANSITIONS: dict[str, set[str]] = {
    # DRAFT→ACTIVE 제거(보안검토 2026-06-29): 결재선·직무분리 우회 차단. 발효는 APPROVED 경유만.
    # 승인 불요 계약은 register가 처음부터 ACTIVE 생성(activate 미경유).
    DRAFT: {PENDING_APPROVAL, APPROVED, REJECTED},
    PENDING_APPROVAL: {APPROVED, REJECTED},
    APPROVED: {ACTIVE, REJECTED},
    ACTIVE: {EXPIRING, EXPIRED, TERMINATED},
    EXPIRING: {ACTIVE, EXPIRED, TERMINATED},
    EXPIRED: {ACTIVE, TERMINATED},     # 갱신으로 부활
    REJECTED: set(),
    TERMINATED: set(),
}
ACCRUABLE = {ACTIVE, EXPIRING}         # 비용/수익 발생 가능 상태
EXPIRING_SOON_DAYS = 60


def assert_transition(current: str, to: str) -> None:
    """current→to 전이가 허용되지 않으면 ValidationError("전이")."""
    if to not in TRANSITIONS.get(current, set()):
        raise ValidationError(f"허용되지 않은 계약 상태 전이: {current}→{to}")
