"""0180 — 벤더 계약 관리 확대.

계약 관리에 체결일·차기검토일·상대 담당자·내부 담당자·담당 부서·사유를 추가(요청).
기존 start_on(발효)·end_on(만기)·amount·sla_note·note는 유지.

Revision ID: 0180
"""
import sqlalchemy as sa
from alembic import op

revision = "0180"
down_revision = "0179"
branch_labels = None
depends_on = None

_COLS = [
    ("signed_on", sa.Date()),
    ("next_review_on", sa.Date()),
    ("counterparty", sa.String(80)),
    ("counterparty_contact", sa.String(120)),
    ("owner_employee_id", sa.BigInteger()),
    ("department_id", sa.BigInteger()),
    ("reason", sa.Text()),
]


def upgrade() -> None:
    for name, type_ in _COLS:
        op.add_column("vendor_contracts", sa.Column(name, type_), schema="itam")


def downgrade() -> None:
    for name, _ in reversed(_COLS):
        op.drop_column("vendor_contracts", name, schema="itam")
