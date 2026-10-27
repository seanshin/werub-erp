"""0244 — 정기업무(주/월/분기) 레지스트리 + 실행이력.

common.recurring_tasks(정의)·common.recurring_task_runs(실행이력·UNIQUE(task_code,period_key)).
기획서 docs/01-plan/erp-recurring-operations-plan-2026-07-25.md (M1).

Revision ID: 0244
"""
import sqlalchemy as sa
from alembic import op

revision = "0244"
down_revision = "0243"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recurring_tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("domain", sa.String(20), nullable=False),
        sa.Column("category", sa.String(10), nullable=False, server_default="RECURRING"),
        sa.Column("cadence", sa.String(10), nullable=False),
        sa.Column("anchor", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("execution_mode", sa.String(12), nullable=False, server_default="MANUAL"),
        sa.Column("handler_ref", sa.String(64), nullable=True),
        sa.Column("owner_role", sa.String(30), nullable=True),
        sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sla_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("source_ref", sa.String(120), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.String(400), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("cadence IN ('DAILY','WEEKLY','MONTHLY','QUARTERLY','YEARLY')",
                           name="ck_rectask_cadence"),
        sa.CheckConstraint("execution_mode IN ('AUTO','AI_ASSISTED','MANUAL')",
                           name="ck_rectask_mode"),
        sa.CheckConstraint("category IN ('FIXED','RECURRING')", name="ck_rectask_category"),
        schema="common",
    )
    op.create_table(
        "recurring_task_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("task_code", sa.String(64), nullable=False),
        sa.Column("period_key", sa.String(20), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="DUE"),
        sa.Column("triggered_by", sa.String(12), nullable=True),
        sa.Column("actor", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(120), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("decision_key", sa.String(64), nullable=True),   # AI_ASSISTED 승인 연결
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("task_code", "period_key", name="uq_rectask_run_period"),
        sa.CheckConstraint(
            "status IN ('DUE','RUNNING','DONE','FAILED','SKIPPED','WAITING_APPROVAL')",
            name="ck_rectask_run_status"),
        schema="common",
    )


def downgrade() -> None:
    op.drop_table("recurring_task_runs", schema="common")
    op.drop_table("recurring_tasks", schema="common")
