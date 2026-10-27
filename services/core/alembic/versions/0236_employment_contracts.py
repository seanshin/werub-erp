"""0236 — 일반직 근로계약 (근기법 §17·P3-5).

비의료진 근로조건 명시(임금·소정근로시간·근무일·취업장소·업무)·이력. 직원당 ACTIVE 1건.
DRAFT→ACTIVE→ENDED. hr.employment_contracts.

Revision ID: 0236
"""
import sqlalchemy as sa
from alembic import op

revision = "0236"
down_revision = "0235"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "employment_contracts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("employee_id", sa.BigInteger(), sa.ForeignKey("hr.employees.id"),
                  nullable=False, index=True),
        sa.Column("contract_type", sa.String(12), nullable=False, server_default="REGULAR"),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="DRAFT", index=True),
        sa.Column("base_pay", sa.Numeric(12, 0), nullable=False),
        sa.Column("weekly_hours", sa.Numeric(5, 1), nullable=True),
        sa.Column("work_schedule", sa.String(200), nullable=True),
        sa.Column("work_location", sa.String(120), nullable=True),
        sa.Column("job_duties", sa.String(200), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("contract_type IN ('REGULAR','FIXED_TERM','PART_TIME')",
                           name="ck_emp_contract_type"),
        sa.CheckConstraint("status IN ('DRAFT','ACTIVE','ENDED')", name="ck_emp_contract_status"),
        schema="hr",
    )


def downgrade() -> None:
    op.drop_table("employment_contracts", schema="hr")
