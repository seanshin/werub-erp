"""0118 — 종별가산율 master(G6 적용 프레임워크): institution_surcharge_rates.

HIS BASE_CATALOG(기본수가) 동봉(#75) → ERP가 종별가산율 적용(계산 SoT=ERP).
종별×가산율, 적용대상 code_type(기본 TARIFF=행위료). rate 미적재 시 무영향(기본 0).

Revision ID: 0118
"""
import sqlalchemy as sa
from alembic import op

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "institution_surcharge_rates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("grade", sa.String(length=20), nullable=False, unique=True),  # CLINIC|HOSPITAL|GENERAL|SUPERIOR
        sa.Column("rate", sa.Numeric(4, 3), nullable=False),   # 가산율(예: 0.150)
        sa.Column("applies_to", sa.String(length=40), server_default="TARIFF"),  # 적용 code_type(쉼표)
        sa.Column("active", sa.Boolean(), server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="regulatory",
    )


def downgrade() -> None:
    op.drop_table("institution_surcharge_rates", schema="regulatory")
