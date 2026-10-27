"""0103 — forward-looking ECL 오버레이(K-IFRS 1109): allowance_rates OVERLAY 기본 1.0.

연령기반 기대신용손실에 거시전망 오버레이 계수(경기조정)를 곱해 forward-looking 반영.

Revision ID: 0103
"""
from alembic import op

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO finance.allowance_rates (kind, bucket, rate) VALUES ('AR', 'OVERLAY', 1.0) "
        "ON CONFLICT (kind, bucket) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM finance.allowance_rates WHERE kind='AR' AND bucket='OVERLAY'")
