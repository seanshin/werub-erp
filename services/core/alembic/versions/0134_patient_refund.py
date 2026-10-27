"""환불(과오납 반환) — patient.refunds (원무·수납 심화).

수납과 분리된 1급 환불 거래 + 즉시 역전표. 발생주의 ON이면 DR 1131/CR 1110, OFF면 DR 4110/CR 1110.
"""
from alembic import op
import sqlalchemy as sa

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refunds",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("reception_id", sa.BigInteger(), sa.ForeignKey("patient.receptions.id")),
        sa.Column("chart_no", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(14, 0), nullable=False),
        sa.Column("method", sa.String(10), nullable=False, server_default="CASH"),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("refunded_on", sa.Date(), nullable=False),
        sa.Column("voucher_ref", sa.String(64)),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="patient",
    )
    op.create_index("ix_refunds_chart", "refunds", ["chart_no"], schema="patient")
    op.create_index("ix_refunds_on", "refunds", ["refunded_on"], schema="patient")


def downgrade() -> None:
    op.drop_index("ix_refunds_on", table_name="refunds", schema="patient")
    op.drop_index("ix_refunds_chart", table_name="refunds", schema="patient")
    op.drop_table("refunds", schema="patient")
