"""0235 — AP 지급결의 배치 (펌뱅킹 이체·P3-4).

미지급금 다건 → 배치 결의(DRAFT)→승인(SoD)→실행(전표 DR2110/CR1110·이체파일). 라인이
SettlementService.pay 재사용. disbursement_batches·disbursement_lines.

Revision ID: 0235
"""
import sqlalchemy as sa
from alembic import op

revision = "0235"
down_revision = "0234"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "disbursement_batches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("batch_no", sa.String(24), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="DRAFT", index=True),
        sa.Column("scheduled_on", sa.Date(), nullable=False),
        sa.Column("total_amount", sa.Numeric(16, 0), nullable=False, server_default="0"),
        sa.Column("line_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("executed_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("status IN ('DRAFT','APPROVED','EXECUTED','CANCELED')",
                           name="ck_disb_status"),
        sa.UniqueConstraint("batch_no", name="uq_disb_batch_no"),
        schema="finance",
    )
    op.create_table(
        "disbursement_lines",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("batch_id", sa.BigInteger(),
                  sa.ForeignKey("finance.disbursement_batches.id"), nullable=False, index=True),
        sa.Column("payable_id", sa.BigInteger(),
                  sa.ForeignKey("finance.payables.id"), nullable=False, index=True),
        sa.Column("party", sa.String(120), nullable=False),
        sa.Column("party_ref", sa.String(40), nullable=True),
        sa.Column("amount", sa.Numeric(14, 0), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("bank_account", sa.String(60), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="PENDING"),
        schema="finance",
    )


def downgrade() -> None:
    op.drop_table("disbursement_lines", schema="finance")
    op.drop_table("disbursement_batches", schema="finance")
