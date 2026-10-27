"""claims.claim_payments — 공단 지급 대사(보험미수금 1140 생애주기 완결).

심사청구 회계가 발생(claim.submitted DR1140)·삭감(claim.reviewed CR1140)까지만 있고 공단 실지급
(입금→AR 소멸)이 없어 보험미수금이 무한 누적되던 갭. 지급명세(payment_ref) 단위로 입금 대사 기록
+ DR 1110/CR 1140 전기, decided(결정액) 대비 variance(과소지급) 산출.
"""
import sqlalchemy as sa
from alembic import op

revision = "0155"
down_revision = "0154"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "claim_payments",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("payment_ref", sa.String(length=60), nullable=False),  # 지급명세 식별(멱등 사업키)
        sa.Column("care_month", sa.String(length=7), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("paid_amt", sa.Numeric(14, 0), nullable=False),
        sa.Column("expected_amt", sa.Numeric(14, 0), nullable=True),   # 결정액 스냅샷(대사 기준)
        sa.Column("variance", sa.Numeric(14, 0), nullable=True),       # expected-paid(양수=과소지급)
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("voucher_ref", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("payment_ref", name="uq_claim_payment_ref"),
        schema="claims",
    )
    op.create_index("ix_claim_payments_care_month", "claim_payments", ["care_month"],
                    schema="claims")


def downgrade() -> None:
    op.drop_index("ix_claim_payments_care_month", table_name="claim_payments", schema="claims")
    op.drop_table("claim_payments", schema="claims")
