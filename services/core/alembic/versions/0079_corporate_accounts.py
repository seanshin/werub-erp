"""0079 — 법인 자금계좌 마스터 + 외화 GL 계정(외화예금·외환차익/차손).

외화는 원화로 섞지 않고 통화별 별도 계좌로 관리(Stage 1: 마스터+화면). 외화 GL 계정 신설:
1115 외화예금(ASSET)·4630 외환차익(REVENUE)·5660 외환차손(EXPENSE). 외화 voucher 라우팅은 Stage 2.

Revision ID: 0079
"""
from alembic import op
import sqlalchemy as sa

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES
            ('1115','외화예금','ASSET'),
            ('4630','외환차익','REVENUE'),
            ('5660','외환차손','EXPENSE')
        ON CONFLICT (code) DO NOTHING
    """)
    op.create_table(
        "corporate_accounts",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("alias", sa.String(60), nullable=False),
        sa.Column("bank_name", sa.String(60), nullable=False),
        sa.Column("account_no", sa.String(40), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="KRW"),
        sa.Column("gl_account_code", sa.String(20), nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("holder", sa.String(60)),
        sa.Column("swift", sa.String(16)),
        sa.Column("note", sa.String(200)),
        sa.Column("updated_by", sa.BigInteger),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("bank_name", "account_no", name="uq_corp_acct"),
        schema="finance",
    )


def downgrade() -> None:
    op.drop_table("corporate_accounts", schema="finance")
