"""0206 — 계정 잔액 체크포인트(성능 H-2): finance.account_balance_checkpoints.

balance_sheet의 개시이래 전 원장 스캔을 `체크포인트 + 당기 델타`로 축소(대규모 성장 대비). 배치가 마감월
경계마다 계정별 누적 차/대변합을 적재. 자기검증(체크포인트+델타 == 전구간)으로 정확성 보장·미적재 시
현행(전구간) 폴백. append/upsert 성격이나 재적재(경계 재계산) 허용이라 REVOKE/트리거 없음.

Revision ID: 0206
"""
import sqlalchemy as sa
from alembic import op

revision = "0206"
down_revision = "0205"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_balance_checkpoints",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("through", sa.Date(), nullable=False),
        sa.Column("account_code", sa.String(20), nullable=False),
        sa.Column("cum_debit", sa.Numeric(20, 0), nullable=False),
        sa.Column("cum_credit", sa.Numeric(20, 0), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("through", "account_code", name="uq_balchkpt"),
        schema="finance",
    )
    op.create_index("ix_balchkpt_through", "account_balance_checkpoints", ["through"],
                    schema="finance")


def downgrade() -> None:
    op.drop_index("ix_balchkpt_through", "account_balance_checkpoints", schema="finance")
    op.drop_table("account_balance_checkpoints", schema="finance")
