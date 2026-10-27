"""2027 전환 대비 (2026-10-11).

- 공휴일 후보 검토 기록 — 노동절·제헌절(2026 관공서 공휴일)·선거일·2027 공휴일을 후보로 두고 사람이 원문 대조 후 확정.
- 법정 파라미터 kind 'TAX_AMEND' — 세법 개정분만 원본 연도 위에 등록(종전: 개정 시 코드 변경 필요).

Revision ID: 0264
Revises: 0263
"""
import sqlalchemy as sa
from alembic import op

revision = "0264"
down_revision = "0263"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "holiday_candidate_reviews",
        sa.Column("year", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("registered", sa.JSON, nullable=False),
        sa.Column("excluded", sa.JSON, nullable=False),
        sa.Column("source_note", sa.String(500), nullable=False),
        sa.Column("reviewed_by", sa.BigInteger),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="common",
    )
    op.drop_constraint("ck_statutory_params_kind", "statutory_params", schema="hr", type_="check")
    op.create_check_constraint("ck_statutory_params_kind", "statutory_params",
                               "kind IN ('INSURANCE','TAX_CARRY','TAX_AMEND')", schema="hr")


def downgrade() -> None:
    op.execute("DELETE FROM hr.statutory_params WHERE kind = 'TAX_AMEND'")
    op.drop_constraint("ck_statutory_params_kind", "statutory_params", schema="hr", type_="check")
    op.create_check_constraint("ck_statutory_params_kind", "statutory_params",
                               "kind IN ('INSURANCE','TAX_CARRY')", schema="hr")
    op.drop_table("holiday_candidate_reviews", schema="common")
