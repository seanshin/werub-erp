"""0018 — hr.medical_contracts (의료진 개별 계약).

P4-1: 계약별 기본급·성과급 산식·당직. 직원당 ACTIVE 1건(부분 unique).

Revision ID: 0018
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE hr.medical_contracts (
            id BIGSERIAL PRIMARY KEY,
            employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
            valid_from DATE NOT NULL,
            valid_to DATE,
            base_pay NUMERIC(12,0) NOT NULL,
            duty_pay NUMERIC(10,0),
            incentive_type VARCHAR(16) NOT NULL DEFAULT 'NONE'
                CHECK (incentive_type IN ('NONE','NET_COLLECTION','DEPT_SHARE','TARGET_EXCESS','RVU')),
            incentive_rate NUMERIC(5,2),
            incentive_params JSONB NOT NULL DEFAULT '{}'::jsonb,
            exclusive BOOLEAN NOT NULL DEFAULT TRUE,
            status VARCHAR(8) NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE','ENDED','DRAFT')),
            note TEXT,
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_mc_employee ON hr.medical_contracts (employee_id)")
    # 직원당 ACTIVE 계약 1건만
    op.execute("CREATE UNIQUE INDEX uq_mc_active ON hr.medical_contracts (employee_id) "
               "WHERE status = 'ACTIVE'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS hr.medical_contracts")
