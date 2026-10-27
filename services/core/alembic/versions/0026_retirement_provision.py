"""0026 — 퇴직급여충당부채: 계정(2120·5320) + 월별 충당 적립 내역.

월별 충당: 전 직원 퇴직금추계액 증가분을 퇴직급여비용(5320)/퇴직급여충당부채(2120)로 적립.

Revision ID: 0026
"""
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for code, name, kind in [("2120", "퇴직급여충당부채", "LIABILITY"),
                             ("5320", "퇴직급여비용", "EXPENSE")]:
        op.execute(f"""
            INSERT INTO finance.accounts (code, name, kind)
            SELECT '{code}', '{name}', '{kind}'
            WHERE NOT EXISTS (SELECT 1 FROM finance.accounts WHERE code = '{code}')
        """)
    op.execute("""
        CREATE TABLE hr.retirement_provisions (
            id BIGSERIAL PRIMARY KEY,
            employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
            period VARCHAR(7) NOT NULL,
            avg_wage NUMERIC(14,0) NOT NULL DEFAULT 0,
            estimate NUMERIC(16,0) NOT NULL DEFAULT 0,
            increment NUMERIC(16,0) NOT NULL DEFAULT 0,
            cumulative NUMERIC(16,0) NOT NULL DEFAULT 0,
            voucher_ref VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_retire_prov UNIQUE (employee_id, period)
        )
    """)
    op.execute("CREATE INDEX ix_retire_period ON hr.retirement_provisions (period)")


def downgrade() -> None:
    op.execute("DROP TABLE hr.retirement_provisions")
    op.execute("DELETE FROM finance.accounts WHERE code IN ('2120','5320')")
