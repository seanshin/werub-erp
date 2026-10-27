"""0072 — 딜러 세무·송금 마스터(scm.suppliers).

HIS B2-a: vendor.synced payload에 taxId(사업자번호/TIN)·bankInfo({account,bank,swift})
동봉 시작 → 정산 AP 송금에 사용. ADD COLUMN IF NOT EXISTS로 idempotent.

Revision ID: 0072
"""
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None

_COLS = [
    ("tax_id", "VARCHAR(40)"),
    ("bank_account", "VARCHAR(40)"),
    ("bank_name", "VARCHAR(60)"),
    ("bank_swift", "VARCHAR(16)"),
]


def upgrade() -> None:
    for name, ddl in _COLS:
        op.execute(f"ALTER TABLE scm.suppliers ADD COLUMN IF NOT EXISTS {name} {ddl}")


def downgrade() -> None:
    for name, _ in _COLS:
        op.execute(f"ALTER TABLE scm.suppliers DROP COLUMN IF EXISTS {name}")
