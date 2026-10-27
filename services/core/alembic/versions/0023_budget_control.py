"""0023 — finance 예산 통제 고도화: 약정(encumbrance)·예산 변경 이력.

Revision ID: 0023
"""
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE finance.encumbrances (
            id BIGSERIAL PRIMARY KEY,
            department_id BIGINT NOT NULL,
            fiscal_year INTEGER NOT NULL,
            account_code VARCHAR(20) NOT NULL,
            amount NUMERIC(14,0) NOT NULL,
            description VARCHAR(200),
            source_ref VARCHAR(64),
            status VARCHAR(10) NOT NULL DEFAULT 'OPEN',
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            released_at TIMESTAMPTZ,
            CONSTRAINT ck_enc_status CHECK (status IN ('OPEN','RELEASED'))
        )
    """)
    op.execute("CREATE INDEX ix_enc_budget ON finance.encumbrances "
               "(fiscal_year, department_id, account_code, status)")
    op.execute("CREATE UNIQUE INDEX uq_enc_source ON finance.encumbrances (source_ref) "
               "WHERE source_ref IS NOT NULL")
    op.execute("""
        CREATE TABLE finance.budget_amendments (
            id BIGSERIAL PRIMARY KEY,
            department_id BIGINT NOT NULL,
            fiscal_year INTEGER NOT NULL,
            account_code VARCHAR(20) NOT NULL,
            old_amount NUMERIC(14,0),
            new_amount NUMERIC(14,0) NOT NULL,
            reason VARCHAR(200),
            changed_by BIGINT,
            changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_budamd ON finance.budget_amendments "
               "(fiscal_year, department_id, account_code)")


def downgrade() -> None:
    op.execute("DROP TABLE finance.budget_amendments")
    op.execute("DROP TABLE finance.encumbrances")
