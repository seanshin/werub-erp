"""0022 — finance.fiscal_periods (월 결산 마감·기간 잠금).

Revision ID: 0022
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE finance.fiscal_periods (
            yyyymm VARCHAR(7) PRIMARY KEY,
            fiscal_year INTEGER NOT NULL,
            status VARCHAR(8) NOT NULL DEFAULT 'OPEN',
            total_revenue NUMERIC(16,2),
            total_expense NUMERIC(16,2),
            net_income NUMERIC(16,2),
            voucher_count INTEGER,
            closed_by BIGINT,
            closed_at TIMESTAMPTZ,
            note VARCHAR(200),
            CONSTRAINT ck_period_status CHECK (status IN ('OPEN','CLOSED'))
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE finance.fiscal_periods")
