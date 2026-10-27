"""0229 — 인사평가 (Tier3): 평가 차수·항목(가중치)·평가·항목별 점수.

연간/반기 성과·역량·MBO 평가. 차수(cycle)별 항목 템플릿 → 대상 직원 평가 배정 → 자기평가→
평가자 채점→확정(가중 총점·등급). 인증평가/승진·성과급 근거.

- eval_cycles: 평가 차수(기간·유형·상태 DRAFT/OPEN/CLOSED).
- eval_criteria: 차수별 평가 항목(역량/실적/MBO·가중치).
- evaluations: 대상직원×차수(평가자·상태·총점·등급).
- eval_scores: 평가×항목(자기점수·평가자점수).

Revision ID: 0229
"""
import sqlalchemy as sa
from alembic import op

revision = "0229"
down_revision = "0228"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eval_cycles",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("period", sa.String(16), nullable=False),        # 2026·2026-H1 등
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("cycle_type", sa.String(10), nullable=False, server_default="ANNUAL"),
        sa.Column("start_on", sa.Date(), nullable=True),
        sa.Column("end_on", sa.Date(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="DRAFT", index=True),
        sa.Column("grade_bands", sa.JSON(), nullable=True),        # [{grade,min}] 총점→등급
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("period", name="uq_eval_cycle_period"),
        sa.CheckConstraint("cycle_type IN ('ANNUAL','HALF','QUARTER')", name="ck_evcycle_type"),
        sa.CheckConstraint("status IN ('DRAFT','OPEN','CLOSED')", name="ck_evcycle_status"),
        schema="hr",
    )
    op.create_table(
        "eval_criteria",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("cycle_id", sa.BigInteger(), sa.ForeignKey("hr.eval_cycles.id"),
                  nullable=False, index=True),
        sa.Column("kind", sa.String(12), nullable=False, server_default="COMPETENCY"),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("weight", sa.Numeric(5, 2), nullable=False, server_default="1"),
        sa.Column("max_score", sa.Numeric(5, 1), nullable=False, server_default="100"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("kind IN ('COMPETENCY','PERFORMANCE','MBO')", name="ck_evcrit_kind"),
        schema="hr",
    )
    op.create_table(
        "evaluations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("cycle_id", sa.BigInteger(), sa.ForeignKey("hr.eval_cycles.id"),
                  nullable=False, index=True),
        sa.Column("employee_id", sa.BigInteger(), sa.ForeignKey("hr.employees.id"),
                  nullable=False, index=True),
        sa.Column("evaluator_id", sa.BigInteger(), nullable=True),   # 1차 평가자(user id)
        sa.Column("status", sa.String(12), nullable=False, server_default="PENDING", index=True),
        sa.Column("self_total", sa.Numeric(6, 2), nullable=True),
        sa.Column("eval_total", sa.Numeric(6, 2), nullable=True),
        sa.Column("final_grade", sa.String(4), nullable=True),
        sa.Column("evaluator_comment", sa.Text(), nullable=True),
        sa.Column("self_comment", sa.Text(), nullable=True),
        sa.Column("confirmed_by", sa.BigInteger(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("cycle_id", "employee_id", name="uq_evaluation"),
        sa.CheckConstraint(
            "status IN ('PENDING','SELF_DONE','EVAL_DONE','CONFIRMED')", name="ck_eval_status"),
        schema="hr",
    )
    op.create_table(
        "eval_scores",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("evaluation_id", sa.BigInteger(), sa.ForeignKey("hr.evaluations.id"),
                  nullable=False, index=True),
        sa.Column("criterion_id", sa.BigInteger(), sa.ForeignKey("hr.eval_criteria.id"),
                  nullable=False),
        sa.Column("self_score", sa.Numeric(5, 1), nullable=True),
        sa.Column("eval_score", sa.Numeric(5, 1), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.UniqueConstraint("evaluation_id", "criterion_id", name="uq_eval_score"),
        schema="hr",
    )


def downgrade() -> None:
    op.drop_table("eval_scores", schema="hr")
    op.drop_table("evaluations", schema="hr")
    op.drop_table("eval_criteria", schema="hr")
    op.drop_table("eval_cycles", schema="hr")
