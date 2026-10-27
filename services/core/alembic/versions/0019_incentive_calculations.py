"""0019 — hr.incentive_calculations (성과급 산정·승인).

P4-4: 산정→검토→승인→취소(claw-back). 직원·기간 1건. 승인 시 PayrollLine+미지급 전표.

Revision ID: 0019
"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE hr.incentive_calculations (
            id BIGSERIAL PRIMARY KEY,
            employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
            period VARCHAR(7) NOT NULL,
            contract_id BIGINT,
            net_revenue NUMERIC(14,0) NOT NULL DEFAULT 0,
            amount NUMERIC(12,0) NOT NULL DEFAULT 0,
            status VARCHAR(10) NOT NULL DEFAULT 'DRAFT'
                CHECK (status IN ('DRAFT','REVIEWED','APPROVED','CANCELED')),
            snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            voucher_ref VARCHAR(40),
            note TEXT,
            reviewed_by BIGINT, reviewed_at TIMESTAMPTZ,
            approved_by BIGINT, approved_at TIMESTAMPTZ,
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_incentive_period UNIQUE (employee_id, period)
        )
    """)
    op.execute("CREATE INDEX ix_incentive_status ON hr.incentive_calculations (status, period)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS hr.incentive_calculations")
