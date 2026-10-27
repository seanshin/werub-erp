"""결정·조치함 분류 정리 — '일반'·'아키텍처'에 묻힌 업무 결정을 담당 분류로 (2026-10-11).

급여 결정 일부가 GENERAL, 회계 정책 일부가 ARCH에 있어 분류별로 보면 인사·급여·재무 결정이 빠져 보였다.
결정 내용(질문·선택지·결정)은 건드리지 않고 분류만 옮긴다 — 현재 분류가 예전 값일 때만(관리자가 이미 옮겼으면 그대로).

Revision ID: 0263
Revises: 0262
"""
import sqlalchemy as sa
from alembic import op

revision = "0263"
down_revision = "0262"
branch_labels = None
depends_on = None

# key → (예전 분류, 새 분류)
_MOVES = {
    "statutory-2026-rates-check": ("GENERAL", "HR"),
    "payroll-proration-rule": ("GENERAL", "HR"),
    "retirement-income-tax-params-check": ("GENERAL", "HR"),
    "patient-accrual-cutover": ("ARCH", "FINANCE"),
    "g10-xbrl-regulatory": ("ARCH", "FINANCE"),
    "ecosystem-notice-disclosure": ("GENERAL", "SECURITY"),
}


def _move(direction: int) -> None:
    bind = op.get_bind()
    for key, pair in _MOVES.items():
        old, new = pair if direction > 0 else pair[::-1]
        bind.execute(sa.text("UPDATE common.pending_decisions SET category = :new WHERE key = :k AND category = :old"),
                     {"new": new, "k": key, "old": old})


def upgrade() -> None:
    _move(1)


def downgrade() -> None:
    _move(-1)
