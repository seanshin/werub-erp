"""0081 — 사전심사 학습 사례(AI 학습 데이터) 등록·관리.

결정된 적발(인정=삭감위험/오탐=정상)을 라벨된 학습 케이스로 자동수확+수동등록.
품질 승인·의료진 확인분만 학습 적격으로 내보내기(JSONL).

Revision ID: 0081
"""
from alembic import op
import sqlalchemy as sa

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pre_review_learning_cases",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("source_finding_id", sa.BigInteger()),
        sa.Column("chart_no", sa.String(20)),
        sa.Column("care_month", sa.String(7)),
        sa.Column("encounter_type", sa.String(12)),
        sa.Column("rule_code", sa.String(30)),
        sa.Column("rule_type", sa.String(20)),
        sa.Column("code", sa.String(20)),
        sa.Column("diagnosis_codes", sa.String(120)),
        sa.Column("amount", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("label", sa.String(16), nullable=False),
        sa.Column("rationale", sa.Text()),
        sa.Column("physician_confirmed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("confirmed_by_employee", sa.BigInteger()),
        sa.Column("features", sa.JSON()),
        sa.Column("tags", sa.String(200)),
        sa.Column("quality", sa.String(10), nullable=False, server_default="DRAFT"),
        sa.Column("source", sa.String(8), nullable=False, server_default="AUTO"),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("source_finding_id", name="uq_prlc_finding"),
        sa.CheckConstraint("label IN ('REDUCTION','FALSE_POSITIVE')", name="ck_prlc_label"),
        sa.CheckConstraint("quality IN ('DRAFT','APPROVED','REJECTED')", name="ck_prlc_quality"),
        sa.CheckConstraint("source IN ('AUTO','MANUAL')", name="ck_prlc_source"),
        schema="claims",
    )
    op.create_index("ix_prlc_label_quality", "pre_review_learning_cases",
                    ["label", "quality"], schema="claims")


def downgrade() -> None:
    op.drop_index("ix_prlc_label_quality", table_name="pre_review_learning_cases",
                  schema="claims")
    op.drop_table("pre_review_learning_cases", schema="claims")
