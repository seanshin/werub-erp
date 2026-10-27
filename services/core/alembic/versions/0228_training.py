"""0228 — 원내 교육이수 (Tier3): 과정 마스터·회차·이수 기록.

기존 compliance_records(법정의무교육 6종 고정)와 별개 — 임의 사내 과정(신규자 오리엔테이션·감염관리·
심폐소생술·의료기기 사용·부서 직무교육 등)의 과정 마스터·회차 편성·이수/수료·미이수 경보.

- training_courses: 과정 마스터(코드·분류·주기·대상·필수여부).
- training_sessions: 회차(개최일·시간·강사·방식).
- training_completions: 이수 기록(직원×회차·점수·수료증·주기 만료).

Revision ID: 0228
"""
import sqlalchemy as sa
from alembic import op

revision = "0228"
down_revision = "0227"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "training_courses",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("category", sa.String(16), nullable=False, server_default="JOB"),
        sa.Column("cycle_months", sa.Integer(), nullable=False, server_default="0"),  # 0=일회성
        sa.Column("target", sa.String(16), nullable=False, server_default="ALL"),
        sa.Column("target_value", sa.String(40), nullable=True),   # JOB_CATEGORY/DEPT 값
        sa.Column("mandatory", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("code", name="uq_training_course_code"),
        sa.CheckConstraint(
            "category IN ('STATUTORY','JOB','SAFETY','INFECTION','ONBOARDING','QUALITY','OTHER')",
            name="ck_tcourse_category"),
        sa.CheckConstraint("target IN ('ALL','JOB_CATEGORY','DEPT')", name="ck_tcourse_target"),
        schema="hr",
    )
    op.create_table(
        "training_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("course_id", sa.BigInteger(), sa.ForeignKey("hr.training_courses.id"),
                  nullable=False, index=True),
        sa.Column("held_on", sa.Date(), nullable=False, index=True),
        sa.Column("hours", sa.Numeric(4, 1), nullable=False, server_default="1"),
        sa.Column("method", sa.String(10), nullable=False, server_default="OFFLINE"),
        sa.Column("instructor", sa.String(80), nullable=True),
        sa.Column("location", sa.String(120), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("method IN ('OFFLINE','ONLINE','EXTERNAL')", name="ck_tsession_method"),
        schema="hr",
    )
    op.create_table(
        "training_completions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("session_id", sa.BigInteger(), sa.ForeignKey("hr.training_sessions.id"),
                  nullable=False, index=True),
        sa.Column("course_id", sa.BigInteger(), sa.ForeignKey("hr.training_courses.id"),
                  nullable=False, index=True),   # 비정규화(상태 매트릭스 조회 최적화)
        sa.Column("employee_id", sa.BigInteger(), sa.ForeignKey("hr.employees.id"),
                  nullable=False, index=True),
        sa.Column("completed_on", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True, index=True),   # 주기 만료(일회성=NULL)
        sa.Column("score", sa.Numeric(5, 1), nullable=True),
        sa.Column("cert_no", sa.String(40), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("session_id", "employee_id", name="uq_training_completion"),
        schema="hr",
    )


def downgrade() -> None:
    op.drop_table("training_completions", schema="hr")
    op.drop_table("training_sessions", schema="hr")
    op.drop_table("training_courses", schema="hr")
