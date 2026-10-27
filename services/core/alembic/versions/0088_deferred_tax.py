"""0088 — 이연법인세(K-IFRS 1012): 계정·법인세율 설정 시드.

이연법인세자산(1180)·이연법인세부채(2260)·법인세비용(5810)·미지급법인세(2230) 계정 추가 +
finance.tax_rate_settings(유효세율, 기본 20%, 2025-01-01 시행).

Revision ID: 0088
"""
from alembic import op
import sqlalchemy as sa

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None

_ACCOUNTS = [
    ("1180", "이연법인세자산", "ASSET"),
    ("2260", "이연법인세부채", "LIABILITY"),
    ("2230", "미지급법인세", "LIABILITY"),
    ("5810", "법인세비용", "EXPENSE"),
]


def upgrade() -> None:
    op.create_table(
        "tax_rate_settings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("rate", sa.Numeric(6, 4), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="finance",
    )
    op.create_index("ix_finance_tax_rate_settings_valid_from", "tax_rate_settings",
                    ["valid_from"], schema="finance")
    for code, name, kind in _ACCOUNTS:
        op.execute(
            "INSERT INTO finance.accounts (code, name, kind, active) "
            f"VALUES ('{code}', '{name}', '{kind}', true) ON CONFLICT (code) DO NOTHING")
    op.execute(
        "INSERT INTO finance.tax_rate_settings (rate, valid_from) VALUES (0.20, '2025-01-01')")


def downgrade() -> None:
    op.drop_index("ix_finance_tax_rate_settings_valid_from", table_name="tax_rate_settings",
                  schema="finance")
    op.drop_table("tax_rate_settings", schema="finance")
    op.execute("DELETE FROM finance.accounts WHERE code IN ('1180','2260','2230','5810')")
