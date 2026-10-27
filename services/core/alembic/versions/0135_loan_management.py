"""차입금 관리 — 차입금/이자 계정 + finance.loans (자금관리 심화).

단기차입금(2310)·장기차입금(2510)·이자비용(5720, 리스이자 5630과 별도). loans(원금·이율·상환).
ON CONFLICT 멱등.
"""
from alembic import op
import sqlalchemy as sa

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind, active) VALUES
            ('2310','단기차입금','LIABILITY', true),
            ('2510','장기차입금','LIABILITY', true),
            ('5720','이자비용','EXPENSE', true)
        ON CONFLICT (code) DO NOTHING""")
    op.create_table(
        "loans",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("lender", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False, server_default="SHORT"),   # SHORT|LONG
        sa.Column("principal", sa.Numeric(16, 0), nullable=False),
        sa.Column("annual_rate", sa.Numeric(6, 4), nullable=False),                # 0.0000~
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("term_months", sa.Integer(), nullable=False),
        sa.Column("repaid_principal", sa.Numeric(16, 0), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False, server_default="ACTIVE"),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('SHORT','LONG')", name="ck_loan_kind"),
        sa.CheckConstraint("status IN ('ACTIVE','REPAID')", name="ck_loan_status"),
        schema="finance",
    )


def downgrade() -> None:
    op.drop_table("loans", schema="finance")
