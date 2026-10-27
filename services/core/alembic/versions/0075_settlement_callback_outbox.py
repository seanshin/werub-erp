"""0075 — 검진권 정산 지급 회신 아웃박스(ERP→HIS settlement.paid).

DB-4 콜백 송신측 — 정산 부킹 시 enqueue, 송신 job이 HIS 콜백 EP로 POST·백오프 재시도.

Revision ID: 0075
"""
from alembic import op
import sqlalchemy as sa

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dealer_settlement_callbacks",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(80), nullable=False),
        sa.Column("settlement_id", sa.String(64)),
        sa.Column("distributor_id", sa.String(64)),
        sa.Column("amount_paid_krw", sa.Numeric(14, 0), nullable=False),
        sa.Column("withholding_krw", sa.Numeric(14, 0)),
        sa.Column("fx_rate", sa.Numeric(12, 4)),
        sa.Column("erp_voucher_ref", sa.String(64)),
        sa.Column("status", sa.String(8), nullable=False, server_default="QUEUED"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("response_code", sa.Integer),
        sa.Column("error", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("event_id", name="uq_settle_cb_event"),
        sa.CheckConstraint("status IN ('QUEUED','SENT','FAILED','DLQ')", name="ck_settle_cb_status"),
        schema="finance",
    )


def downgrade() -> None:
    op.drop_table("dealer_settlement_callbacks", schema="finance")
