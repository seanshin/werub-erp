"""0009 — finance.receivables·payables (채권/채무) + 미수금 계정.

Revision ID: 0009
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE finance.receivables (
            id BIGSERIAL PRIMARY KEY,
            party VARCHAR(120) NOT NULL,
            description VARCHAR(200),
            amount NUMERIC(14,0) NOT NULL,
            collected_amount NUMERIC(14,0) NOT NULL DEFAULT 0,
            billed_on DATE NOT NULL,
            due_on DATE NOT NULL,
            status VARCHAR(12) NOT NULL DEFAULT 'OUTSTANDING'
                CHECK (status IN ('OUTSTANDING','PARTIAL','COLLECTED')),
            source_module VARCHAR(16) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_ar_due ON finance.receivables (due_on)")
    op.execute("""
        CREATE TABLE finance.payables (
            id BIGSERIAL PRIMARY KEY,
            party VARCHAR(120) NOT NULL,
            description VARCHAR(200),
            amount NUMERIC(14,0) NOT NULL,
            paid_amount NUMERIC(14,0) NOT NULL DEFAULT 0,
            incurred_on DATE NOT NULL,
            due_on DATE NOT NULL,
            status VARCHAR(12) NOT NULL DEFAULT 'OUTSTANDING'
                CHECK (status IN ('OUTSTANDING','PARTIAL','PAID')),
            source_module VARCHAR(16) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_ap_due ON finance.payables (due_on)")
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES ('1130','미수금','ASSET')
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS finance.receivables")
    op.execute("DROP TABLE IF EXISTS finance.payables")
    op.execute("DELETE FROM finance.accounts WHERE code = '1130'")
