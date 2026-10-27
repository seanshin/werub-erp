"""0240 — 직급·호봉 체계 + 징계·상벌 (Tier3·P3-8).

hr.pay_grade_steps(연도별 호봉표)·hr.disciplinary_actions(징계·포상 인사기록).

Revision ID: 0240
"""
import sqlalchemy as sa
from alembic import op

revision = "0240"
down_revision = "0239"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pay_grade_steps",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("grade", sa.String(24), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("base_pay", sa.Numeric(12, 0), nullable=False),
        sa.Column("effective_year", sa.Integer(), nullable=False, index=True),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("grade", "step", "effective_year", name="uq_pay_grade_step"),
        schema="hr",
    )
    op.create_table(
        "disciplinary_actions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("employee_id", sa.BigInteger(), sa.ForeignKey("hr.employees.id"),
                  nullable=False, index=True),
        sa.Column("action_type", sa.String(12), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(400), nullable=False),
        sa.Column("decided_on", sa.Date(), nullable=False, index=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("action_type IN ('DISCIPLINE','REWARD')", name="ck_disc_type"),
        schema="hr",
    )


def downgrade() -> None:
    op.drop_table("disciplinary_actions", schema="hr")
    op.drop_table("pay_grade_steps", schema="hr")
