"""claims.claim_resubmissions — 반송(반려) 청구 재청구 추적.

이의신청(사후 삭감 분쟁)과 별개로, 심사 전 반송된 청구의 정정·재제출을 추적한다. 반송 후 미재청구
청구를 놓쳐 수익을 유실하는 것을 방지(미재청구 조회). 운영 추적 — 회계 전기는 없음(최종 결정은
기존 claim.reviewed 경로).
"""
import sqlalchemy as sa
from alembic import op

revision = "0157"
down_revision = "0156"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "claim_resubmissions",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("original_ref", sa.String(length=40), nullable=False),  # 원 청구 식별(멱등 사업키)
        sa.Column("care_month", sa.String(length=7), nullable=False),
        sa.Column("returned_on", sa.Date(), nullable=False),
        sa.Column("return_code", sa.String(length=20), nullable=True),
        sa.Column("return_reason", sa.Text(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 0), nullable=True),
        sa.Column("resubmit_ref", sa.String(length=40), nullable=True),  # 재청구 접수번호
        sa.Column("resubmitted_on", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="RETURNED"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("original_ref", name="uq_claim_resubmission_ref"),
        sa.CheckConstraint("status IN ('RETURNED','RESUBMITTED','CLOSED')",
                           name="ck_claim_resubmission_status"),
        schema="claims",
    )
    op.create_index("ix_claim_resubmissions_status", "claim_resubmissions", ["status"],
                    schema="claims")
    op.create_index("ix_claim_resubmissions_care_month", "claim_resubmissions", ["care_month"],
                    schema="claims")


def downgrade() -> None:
    op.drop_index("ix_claim_resubmissions_care_month", table_name="claim_resubmissions",
                  schema="claims")
    op.drop_index("ix_claim_resubmissions_status", table_name="claim_resubmissions",
                  schema="claims")
    op.drop_table("claim_resubmissions", schema="claims")
