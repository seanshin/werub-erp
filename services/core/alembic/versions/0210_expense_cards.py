"""0210 — 지출·경비 관리(내부운영 갭 감사 Tier1): 경비청구·법인카드 + 2112 법인카드미지급금.

재무·조달 두 감사가 독립 수렴한 최대 공백(경비정산·법인카드 전무) 보완:
- finance.expense_claims / expense_lines: 직원 경비 기안→결재선→승인(비용/미지급 전기)→지급.
- finance.corporate_cards / card_transactions: 카드 마스터(마지막 4자리만)·승인번호 멱등 적재·
  경비 연결·월정산.
- 계정 2112 법인카드미지급금(개인분 2110과 분리 — 카드사 월정산 대사 축).

Revision ID: 0210
"""
import sqlalchemy as sa
from alembic import op

revision = "0210"
down_revision = "0209"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES
            ('2112','법인카드미지급금','LIABILITY')
        ON CONFLICT (code) DO NOTHING
    """)
    op.create_table(
        "expense_claims",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("employee_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("department_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("claim_on", sa.Date(), nullable=False, index=True),
        sa.Column("total_amount", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False, server_default="DRAFT", index=True),
        sa.Column("voucher_ref", sa.String(64), nullable=True),
        sa.Column("payment_voucher_ref", sa.String(64), nullable=True),
        sa.Column("paid_on", sa.Date(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT','PENDING','APPROVED','REJECTED','PAID','CANCELED')",
            name="ck_expense_status"),
        schema="finance",
    )
    op.create_table(
        "expense_lines",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("claim_id", sa.BigInteger(),
                  sa.ForeignKey("finance.expense_claims.id"), nullable=False, index=True),
        sa.Column("expense_on", sa.Date(), nullable=False),
        sa.Column("account_code", sa.String(20), nullable=False),
        sa.Column("description", sa.String(200), nullable=False),
        sa.Column("merchant", sa.String(120), nullable=True),
        sa.Column("amount", sa.Numeric(14, 0), nullable=False),
        sa.Column("card_tx_id", sa.BigInteger(), nullable=True, index=True),
        schema="finance",
    )
    op.create_table(
        "corporate_cards",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("alias", sa.String(60), nullable=False),
        sa.Column("card_last4", sa.String(4), nullable=False),
        sa.Column("holder_employee_id", sa.BigInteger(), nullable=True),
        sa.Column("department_id", sa.BigInteger(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("alias", name="uq_corp_card_alias"),
        schema="finance",
    )
    op.create_table(
        "card_transactions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("card_id", sa.BigInteger(),
                  sa.ForeignKey("finance.corporate_cards.id"), nullable=False, index=True),
        sa.Column("tx_id", sa.String(60), nullable=False),
        sa.Column("tx_on", sa.Date(), nullable=False, index=True),
        sa.Column("merchant", sa.String(120), nullable=True),
        sa.Column("amount", sa.Numeric(14, 0), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="UNMATCHED",
                  index=True),
        sa.Column("claim_id", sa.BigInteger(), nullable=True, index=True),
        sa.Column("settled_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("tx_id", name="uq_card_tx"),
        sa.CheckConstraint("status IN ('UNMATCHED','CLAIMED','SETTLED')",
                           name="ck_card_tx_status"),
        schema="finance",
    )


def downgrade() -> None:
    op.drop_table("card_transactions", schema="finance")
    op.drop_table("corporate_cards", schema="finance")
    op.drop_table("expense_lines", schema="finance")
    op.drop_table("expense_claims", schema="finance")
    op.execute("DELETE FROM finance.accounts WHERE code = '2112'")
