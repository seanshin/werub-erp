"""0234 — 감염관리 지표 (인증평가·P3-1).

HAI(요로/혈류/수술부위/인공호흡기폐렴)·손위생 수행률·항생제·다제내성균 월별 지표.
분자/분모+배수로 비율 산출·목표대비 추적. (period,type,dept) 고유.

Revision ID: 0234
"""
import sqlalchemy as sa
from alembic import op

revision = "0234"
down_revision = "0233"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "infection_indicators",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("period", sa.String(7), nullable=False, index=True),
        sa.Column("indicator_type", sa.String(16), nullable=False, index=True),
        sa.Column("department_id", sa.BigInteger(), nullable=True),
        sa.Column("numerator", sa.Numeric(14, 2), nullable=False),
        sa.Column("denominator", sa.Numeric(14, 2), nullable=False),
        sa.Column("multiplier", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("target", sa.Numeric(10, 2), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("indicator_type IN ('HAI_UTI','HAI_BSI','HAI_SSI','HAI_VAP',"
                           "'HAND_HYGIENE','ANTIBIOTIC','MDRO')", name="ck_inf_type"),
        sa.UniqueConstraint("period", "indicator_type", "department_id", name="uq_inf_indicator"),
        schema="regulatory",
    )


def downgrade() -> None:
    op.drop_table("infection_indicators", schema="regulatory")
