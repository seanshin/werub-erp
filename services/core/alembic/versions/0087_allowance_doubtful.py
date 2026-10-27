"""0087 — 대손충당금(K-IFRS 1109·1037): 설정 테이블·계정·연령버킷 기본 요율 시드.

대손충당금(1190 자산차감)·대손상각비(5690) 계정 추가 + finance.allowance_rates(연령버킷 ECL 요율).
기본 요율: current 0.5%·1-30 2%·31-60 10%·61-90 30%·90+ 50%(간이 ECL 매트릭스, 관리자 조정).

Revision ID: 0087
"""
from alembic import op
import sqlalchemy as sa

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None

_ACCOUNTS = [
    ("1190", "대손충당금", "ASSET"),
    ("5690", "대손상각비", "EXPENSE"),
]
_RATES = [("current", "0.005"), ("1-30", "0.02"), ("31-60", "0.10"),
          ("61-90", "0.30"), ("90+", "0.50")]


def upgrade() -> None:
    op.create_table(
        "allowance_rates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(length=3), nullable=False, server_default="AR"),
        sa.Column("bucket", sa.String(length=10), nullable=False),
        sa.Column("rate", sa.Numeric(6, 4), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("kind", "bucket", name="uq_allowance_rate"),
        schema="finance",
    )
    for code, name, kind in _ACCOUNTS:
        op.execute(
            "INSERT INTO finance.accounts (code, name, kind, active) "
            f"VALUES ('{code}', '{name}', '{kind}', true) ON CONFLICT (code) DO NOTHING")
    for bucket, rate in _RATES:
        op.execute(
            "INSERT INTO finance.allowance_rates (kind, bucket, rate) "
            f"VALUES ('AR', '{bucket}', {rate}) "
            "ON CONFLICT (kind, bucket) DO NOTHING")


def downgrade() -> None:
    op.drop_table("allowance_rates", schema="finance")
    op.execute("DELETE FROM finance.accounts WHERE code IN ('1190','5690')")
