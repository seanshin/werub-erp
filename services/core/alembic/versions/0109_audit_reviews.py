"""0109 — 감사·이상탐지 후보 검토 상태(ADR D-7): finance.audit_reviews.

finding_key(voucher:<id> | dup:<sha1>)로 재계산 후에도 검토 상태 유지.
Benford(분포 신호)는 항목 검토 대상이 아니라 미포함.

Revision ID: 0109
"""
from alembic import op
import sqlalchemy as sa

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("finding_type", sa.String(length=10), nullable=False),
        sa.Column("finding_key", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=8), server_default="REVIEWED", nullable=False),
        sa.Column("disposition", sa.String(length=8), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("finding_type", "finding_key", name="uq_audit_review"),
        sa.CheckConstraint("finding_type IN ('ABNORMAL','DUPLICATE')",
                           name="ck_audit_finding_type"),
        sa.CheckConstraint("status IN ('OPEN','REVIEWED')", name="ck_audit_review_status"),
        sa.CheckConstraint("disposition IN ('NORMAL','ACTION') OR disposition IS NULL",
                           name="ck_audit_review_disposition"),
        schema="finance",
    )


def downgrade() -> None:
    op.drop_table("audit_reviews", schema="finance")
