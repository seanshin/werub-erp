"""0067 — 품목 바코드(재고 워크스테이션 스캔).

scm.items.barcode(unique, nullable) — 자체발번/GTIN. 미입력 시 code로 폴백.

Revision ID: 0067
"""
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE scm.items ADD COLUMN IF NOT EXISTS barcode VARCHAR(40)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_item_barcode "
               "ON scm.items (barcode) WHERE barcode IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS scm.uq_item_barcode")
    op.execute("ALTER TABLE scm.items DROP COLUMN IF EXISTS barcode")
