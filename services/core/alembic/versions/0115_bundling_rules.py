"""0115 — 묶음청구(P1 G1/G4): bundling_rules.

패키지 수가(parent)에 포함된 세부행위/재료(child)를 별도 청구하면 적발(언번들링).
rule_type=BUNDLE(행위)·MATERIAL(재료 별도산정). 동일 진료일·차트 parent+child 동시청구 시.

Revision ID: 0115
"""
import sqlalchemy as sa
from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bundling_rules",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("parent_code", sa.String(length=20), nullable=False),  # 패키지 수가
        sa.Column("child_codes", sa.String(length=400), nullable=False),  # 포함 세부/재료(쉼표)
        sa.Column("rule_type", sa.String(length=10), server_default="BUNDLE"),  # BUNDLE|MATERIAL
        sa.Column("name", sa.String(length=120)),
        sa.Column("reason", sa.String(length=300)),
        sa.Column("severity", sa.String(length=8), server_default="WARN"),
        sa.Column("active", sa.Boolean(), server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("rule_type IN ('BUNDLE','MATERIAL')", name="ck_bundle_type"),
        schema="claims",
    )
    op.create_index("ix_bundling_parent", "bundling_rules", ["parent_code"], schema="claims")


def downgrade() -> None:
    op.drop_table("bundling_rules", schema="claims")
