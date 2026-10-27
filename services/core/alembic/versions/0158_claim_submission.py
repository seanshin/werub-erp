"""claims.claim_submissions — 제출 청구 캡처(청구액 이중소스 정합).

claim.submitted(HIS webhook)가 보험미수금(1140) AR만 전기하고 청구 자체를 기록하지 않아, MEDRLT
ingest의 청구액(ClaimStatement.claimed_amt)과 대사할 수 없던 갭. 제출 청구를 캡처(claim_id 멱등)해
진료월 단위 제출액 vs 심사대상 청구액 불일치를 탐지(누락·금액차).
"""
import sqlalchemy as sa
from alembic import op

revision = "0158"
down_revision = "0157"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "claim_submissions",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("claim_id", sa.String(length=64), nullable=False),   # 청구 식별(멱등 사업키)
        sa.Column("claim_number", sa.String(length=64), nullable=True),
        sa.Column("care_month", sa.String(length=7), nullable=True),   # 미동봉 시 NULL(미대사 표면화)
        sa.Column("claimed_amt", sa.Numeric(14, 0), nullable=False),
        sa.Column("submitted_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("claim_id", name="uq_claim_submission_id"),
        schema="claims",
    )
    op.create_index("ix_claim_submissions_care_month", "claim_submissions", ["care_month"],
                    schema="claims")


def downgrade() -> None:
    op.drop_index("ix_claim_submissions_care_month", table_name="claim_submissions",
                  schema="claims")
    op.drop_table("claim_submissions", schema="claims")
