"""N단계 결재선 엔진 — approval_rules·approval_instances·approval_steps (결재선 PR-1).

PO·위수탁계약·의료계약 공유 다단계 승인. 룰 미설정 시 도메인은 기존 단일 승인으로 폴백(무중단).
"""
from alembic import op
import sqlalchemy as sa

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_rules",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("doc_type", sa.String(24), nullable=False),
        sa.Column("category", sa.String(24)),
        sa.Column("min_amount", sa.Numeric(16, 0), nullable=False, server_default="0"),
        sa.Column("max_amount", sa.Numeric(16, 0)),
        sa.Column("steps", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="common",
    )
    op.create_index("ix_apprule_doctype", "approval_rules", ["doc_type"], schema="common")
    op.create_table(
        "approval_instances",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("doc_type", sa.String(24), nullable=False),
        sa.Column("doc_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(16, 0), nullable=False, server_default="0"),
        sa.Column("department_id", sa.BigInteger()),
        sa.Column("status", sa.String(10), nullable=False, server_default="PENDING"),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('PENDING','APPROVED','REJECTED')", name="ck_appinst_status"),
        schema="common",
    )
    op.create_index("ix_appinst_doc", "approval_instances", ["doc_type", "doc_id"],
                    schema="common")
    op.create_index("uq_appinst_pending", "approval_instances", ["doc_type", "doc_id"],
                    unique=True, schema="common",
                    postgresql_where=sa.text("status='PENDING'"))
    op.create_table(
        "approval_steps",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("instance_id", sa.BigInteger(),
                  sa.ForeignKey("common.approval_instances.id"), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("approver_type", sa.String(12), nullable=False),
        sa.Column("approver_value", sa.String(40)),
        sa.Column("status", sa.String(10), nullable=False, server_default="PENDING"),
        sa.Column("acted_by", sa.BigInteger()),
        sa.Column("acted_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.String(300)),
        sa.CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','SKIPPED')",
                           name="ck_appstep_status"),
        schema="common",
    )
    op.create_index("ix_appstep_instance", "approval_steps", ["instance_id"], schema="common")


def downgrade() -> None:
    op.drop_table("approval_steps", schema="common")
    op.drop_table("approval_instances", schema="common")
    op.drop_table("approval_rules", schema="common")
