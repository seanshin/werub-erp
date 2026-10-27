"""0162 — 기관 설정(common.org_settings) 단일행 테이블.

공식 문서(증명서·계약서·급여명세) 발급기관 표기의 SoT — 관리화면에서 ADMIN 설정.
env ORG_NAME은 폴백으로 유지(DB > env > "본 의료기관").

Revision ID: 0162
"""
import sqlalchemy as sa
from alembic import op

revision = "0162"
down_revision = "0161"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_settings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("representative", sa.String(80), nullable=True),
        sa.Column("biz_reg_no", sa.String(20), nullable=True),
        sa.Column("care_org_no", sa.String(20), nullable=True),
        sa.Column("address", sa.String(200), nullable=True),
        sa.Column("tel", sa.String(30), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_org_settings_singleton"),
        schema="common",
    )


def downgrade() -> None:
    op.drop_table("org_settings", schema="common")
