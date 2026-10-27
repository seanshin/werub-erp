"""0243 — 채용 ATS (공고·지원자·전형·P3-9·Tier3).

hr.job_postings(공고)·hr.job_applicants(지원자·전형단계). 지원자 개인정보는 hr.recruit 스코프.

Revision ID: 0243
"""
import sqlalchemy as sa
from alembic import op

revision = "0243"
down_revision = "0242"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_postings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("department_id", sa.BigInteger(), nullable=True),
        sa.Column("employment_type", sa.String(12), nullable=False, server_default="REGULAR"),
        sa.Column("headcount", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(10), nullable=False, server_default="OPEN", index=True),
        sa.Column("posted_on", sa.Date(), nullable=False),
        sa.Column("close_on", sa.Date(), nullable=True),
        sa.Column("note", sa.String(400), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("status IN ('OPEN','CLOSED','FILLED')", name="ck_posting_status"),
        schema="hr",
    )
    op.create_table(
        "job_applicants",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("posting_id", sa.BigInteger(), sa.ForeignKey("hr.job_postings.id"),
                  nullable=False, index=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("contact", sa.String(120), nullable=True),
        sa.Column("source", sa.String(40), nullable=True),
        sa.Column("stage", sa.String(12), nullable=False, server_default="APPLIED", index=True),
        sa.Column("applied_on", sa.Date(), nullable=False),
        sa.Column("note", sa.String(400), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("stage IN ('APPLIED','SCREENING','INTERVIEW','OFFER','HIRED','REJECTED')",
                           name="ck_applicant_stage"),
        schema="hr",
    )


def downgrade() -> None:
    op.drop_table("job_applicants", schema="hr")
    op.drop_table("job_postings", schema="hr")
