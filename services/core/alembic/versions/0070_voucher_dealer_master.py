"""0070 — 검진권 딜러 마스터 필드(scm.suppliers) + business_no nullable.

HIS vendor.synced/changed 수신(B-G1-A) — 딜러 거래처 upsert용. 해외 딜러는
국내 사업자번호가 없어 business_no NOT NULL 완화. ADD COLUMN IF NOT EXISTS로 idempotent.

Revision ID: 0070
"""
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None

_COLS = [
    ("vendor_type", "VARCHAR(24)"),
    ("dealer_type", "VARCHAR(12)"),
    ("tier", "VARCHAR(12)"),
    ("commission_rate", "NUMERIC(5,2)"),
    ("country", "VARCHAR(2)"),
    ("currency", "VARCHAR(3)"),
    ("is_active", "BOOLEAN NOT NULL DEFAULT TRUE"),
]


def upgrade() -> None:
    for name, ddl in _COLS:
        op.execute(f"ALTER TABLE scm.suppliers ADD COLUMN IF NOT EXISTS {name} {ddl}")
    op.execute("ALTER TABLE scm.suppliers ALTER COLUMN business_no DROP NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE scm.suppliers ALTER COLUMN business_no SET NOT NULL")
    for name, _ in _COLS:
        op.execute(f"ALTER TABLE scm.suppliers DROP COLUMN IF EXISTS {name}")
