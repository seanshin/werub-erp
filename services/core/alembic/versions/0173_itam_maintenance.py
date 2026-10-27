"""0173 — ITAM M3: 유지보수 이력 + 폐기 데이터 소거 게이트.

itam.maintenance_records(수리·점검·업그레이드·교정) + it_assets에 data_erased·disposed_at 추가
(PHI 장비 소거 확인 후 폐기 통제).

Revision ID: 0173
"""
import sqlalchemy as sa
from alembic import op

revision = "0173"
down_revision = "0172"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("it_assets", sa.Column("data_erased", sa.Boolean), schema="itam")
    op.add_column("it_assets", sa.Column("disposed_at", sa.Date), schema="itam")

    op.create_table(
        "maintenance_records",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("it_asset_id", sa.BigInteger, sa.ForeignKey("itam.it_assets.id"),
                  nullable=False, index=True),
        sa.Column("kind", sa.String(12), nullable=False, server_default="REPAIR"),
        sa.Column("on_date", sa.Date, nullable=False),
        sa.Column("vendor", sa.String(120)),
        sa.Column("cost", sa.Numeric(16, 0)),
        sa.Column("note", sa.Text),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="itam",
    )


def downgrade() -> None:
    op.drop_table("maintenance_records", schema="itam")
    op.drop_column("it_assets", "disposed_at", schema="itam")
    op.drop_column("it_assets", "data_erased", schema="itam")
