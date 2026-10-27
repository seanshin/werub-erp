"""0114 — 진단(KCD)-수가 연계(P1 G2): dx_procedure_rules.

수가코드에 필수 상병(KCD prefix)을 정의 → 환자 상병에 해당 prefix가 없으면 형식 적발(미기재).
임상 타당성 판정은 의료진 게이트(의료법). 결정론 형식 점검(WARN 기본).

Revision ID: 0114
"""
import sqlalchemy as sa
from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dx_procedure_rules",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=20), nullable=False, unique=True),
        sa.Column("require_dx_prefixes", sa.String(length=200)),
        sa.Column("name", sa.String(length=120)),
        sa.Column("reason", sa.String(length=300)),
        sa.Column("severity", sa.String(length=8), server_default="WARN"),
        sa.Column("active", sa.Boolean(), server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="claims",
    )


def downgrade() -> None:
    op.drop_table("dx_procedure_rules", schema="claims")
