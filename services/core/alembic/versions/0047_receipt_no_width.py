"""0047 — claim_batches.receipt_no 폭 확장(20→40).

MEDRLT JSON 채널의 noticeNo(예: TEST-MEDRLT-20260611-001, 24자)가 varchar(20) 초과 —
실물 접수번호·합성 식별자 모두 수용하도록 확장.

Revision ID: 0047
"""
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE claims.claim_batches ALTER COLUMN receipt_no TYPE VARCHAR(40)")


def downgrade() -> None:
    op.execute("ALTER TABLE claims.claim_batches ALTER COLUMN receipt_no TYPE VARCHAR(20)")
