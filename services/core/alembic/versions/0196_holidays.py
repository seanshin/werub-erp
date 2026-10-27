"""0196 — 공휴일 캘린더(P3): common.holidays.

변동 공휴일(음력 명절·대체공휴일)을 코드 하드코딩(claims/deadline.EXTRA_HOLIDAYS·연 1회 코드수정)
대신 관리 가능한 데이터로. 고정 양력·주말은 코드 유지, 본 테이블은 매년 갱신되는 변동분.

Revision ID: 0196
"""
import sqlalchemy as sa
from alembic import op

revision = "0196"
down_revision = "0195"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "holidays",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("on_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(16), server_default="PUBLIC", nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("on_date", name="uq_holiday_date"),
        schema="common",
    )
    op.create_index("ix_holidays_on_date", "holidays", ["on_date"], schema="common")

    # 2026 변동 공휴일 시드(기존 코드 EXTRA_HOLIDAYS 이관·고정 양력·주말은 코드 유지)
    seed = [
        ("2026-02-16", "설 연휴", "LUNAR"), ("2026-02-17", "설날", "LUNAR"),
        ("2026-02-18", "설 연휴", "LUNAR"), ("2026-03-02", "3·1절 대체공휴일", "SUBSTITUTE"),
        ("2026-05-25", "부처님오신날 대체공휴일", "SUBSTITUTE"),
        ("2026-08-17", "광복절 대체공휴일", "SUBSTITUTE"),
        ("2026-09-24", "추석 연휴", "LUNAR"), ("2026-09-25", "추석", "LUNAR"),
        ("2026-10-05", "개천절 대체공휴일", "SUBSTITUTE"),
    ]
    for d, name, kind in seed:
        op.execute(
            f"INSERT INTO common.holidays (on_date, name, kind) VALUES ('{d}', '{name}', '{kind}') "
            f"ON CONFLICT (on_date) DO NOTHING")


def downgrade() -> None:
    op.drop_index("ix_holidays_on_date", table_name="holidays", schema="common")
    op.drop_table("holidays", schema="common")
