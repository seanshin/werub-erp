"""0241 — 진료의뢰·회송 (진료협력센터·P3-3).

의뢰(REFER_OUT)·의뢰접수(REFER_IN)·회송(BACK_REFER) 기록·의뢰회송료·회송률 소스. patient.referrals.

Revision ID: 0241
"""
import sqlalchemy as sa
from alembic import op

revision = "0241"
down_revision = "0240"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "referrals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("referral_type", sa.String(12), nullable=False, index=True),
        sa.Column("partner_institution", sa.String(120), nullable=False),
        sa.Column("chart_no", sa.String(20), nullable=True),
        sa.Column("patient_name", sa.String(80), nullable=True),
        sa.Column("department", sa.String(40), nullable=True),
        sa.Column("referred_on", sa.Date(), nullable=False, index=True),
        sa.Column("reason", sa.String(400), nullable=True),
        sa.Column("has_fee", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False, server_default="OPEN"),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("referral_type IN ('REFER_OUT','REFER_IN','BACK_REFER')",
                           name="ck_referral_type"),
        sa.CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_referral_status"),
        schema="patient",
    )


def downgrade() -> None:
    op.drop_table("referrals", schema="patient")
