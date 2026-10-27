"""0172 — ITAM M2b: 구독·클라우드 비용 원장 + 비용 주기 점검(변동 감지).

itam.service_costs(월 비용·환율 KRW)·itam.cost_reviews(변동 감지·책임자 확인).

Revision ID: 0172
"""
import sqlalchemy as sa
from alembic import op

revision = "0172"
down_revision = "0171"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_costs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("service_id", sa.BigInteger, sa.ForeignKey("itam.software_services.id"),
                  nullable=False, index=True),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="KRW"),
        sa.Column("fx_rate", sa.Numeric(12, 4)),
        sa.Column("amount_krw", sa.Numeric(16, 0), nullable=False),
        sa.Column("usage_note", sa.String(200)),
        sa.Column("source", sa.String(10), nullable=False, server_default="MANUAL"),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("service_id", "period", name="uq_service_cost_period"),
        schema="itam",
    )
    op.create_index("ix_service_costs_period", "service_costs", ["period"], schema="itam")

    op.create_table(
        "cost_reviews",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("period", sa.String(7), nullable=False, index=True),
        sa.Column("service_id", sa.BigInteger, sa.ForeignKey("itam.software_services.id"),
                  nullable=False, index=True),
        sa.Column("prev_amount", sa.Numeric(16, 0)),
        sa.Column("amount", sa.Numeric(16, 0)),
        sa.Column("delta_pct", sa.Numeric(8, 2)),
        sa.Column("reason", sa.String(10)),
        sa.Column("status", sa.String(10), nullable=False, server_default="PENDING"),
        sa.Column("reviewed_by", sa.BigInteger),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text),
        sa.UniqueConstraint("service_id", "period", name="uq_cost_review_period"),
        schema="itam",
    )


def downgrade() -> None:
    op.drop_table("cost_reviews", schema="itam")
    op.drop_table("service_costs", schema="itam")
