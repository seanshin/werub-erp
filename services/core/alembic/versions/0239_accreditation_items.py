"""0239 — 의료기관 인증평가 조사항목 (상시준비·P3-2).

조사기준별 준비상태·근거·담당 등록부. regulatory.accreditation_items.

Revision ID: 0239
"""
import sqlalchemy as sa
from alembic import op

revision = "0239"
down_revision = "0238"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accreditation_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("standard", sa.String(24), nullable=False, server_default="ACUTE"),
        sa.Column("domain", sa.String(12), nullable=False, index=True),
        sa.Column("chapter", sa.String(60), nullable=True),
        sa.Column("item_code", sa.String(24), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("requirement", sa.Text(), nullable=True),
        sa.Column("evidence_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="GAP", index=True),
        sa.Column("owner_dept_id", sa.BigInteger(), nullable=True),
        sa.Column("last_reviewed_on", sa.Date(), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("domain IN ('BASIC','CARE','ADMIN','PERFORMANCE')", name="ck_acc_domain"),
        sa.CheckConstraint("status IN ('READY','PARTIAL','GAP','NA')", name="ck_acc_status"),
        sa.UniqueConstraint("standard", "item_code", name="uq_acc_item"),
        schema="regulatory",
    )


def downgrade() -> None:
    op.drop_table("accreditation_items", schema="regulatory")
