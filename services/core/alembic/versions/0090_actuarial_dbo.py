"""0090 — 퇴직급여 보험수리(K-IFRS 1019): 가정 테이블·보험수리적손익(OCI) 계정.

hr.actuarial_assumptions(할인율·임금상승률·정년) + 보험수리적손익(3410, OCI 자본) 계정.
기본 가정: 할인율 4.0%·임금상승 3.0%·정년 60 (2025-01-01 시행).

Revision ID: 0090
"""
from alembic import op
import sqlalchemy as sa

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "actuarial_assumptions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("discount_rate", sa.Numeric(6, 4), nullable=False),
        sa.Column("salary_growth", sa.Numeric(6, 4), nullable=False),
        sa.Column("retirement_age", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="hr",
    )
    op.create_index("ix_hr_actuarial_assumptions_valid_from", "actuarial_assumptions",
                    ["valid_from"], schema="hr")
    op.execute(
        "INSERT INTO finance.accounts (code, name, kind, active) "
        "VALUES ('3410', '보험수리적손익', 'CAPITAL', true) ON CONFLICT (code) DO NOTHING")
    op.execute(
        "INSERT INTO hr.actuarial_assumptions (discount_rate, salary_growth, retirement_age, valid_from) "
        "VALUES (0.04, 0.03, 60, '2025-01-01')")


def downgrade() -> None:
    op.drop_index("ix_hr_actuarial_assumptions_valid_from", table_name="actuarial_assumptions",
                  schema="hr")
    op.drop_table("actuarial_assumptions", schema="hr")
    op.execute("DELETE FROM finance.accounts WHERE code = '3410'")
