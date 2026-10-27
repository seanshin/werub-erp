"""0086 — 재무 시뮬레이션 시나리오 서버 저장(finance.sim_scenarios).

사용자별 드라이버 가정(drivers JSON)을 서버에 영속 — 기존 localStorage 한계(기기 종속) 해소.
(user_id, name) 고유 → 동일 이름 저장 시 upsert.

Revision ID: 0086
"""
from alembic import op
import sqlalchemy as sa

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sim_scenarios",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("drivers", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "name", name="uq_sim_scenario_user_name"),
        schema="finance",
    )
    op.create_index("ix_finance_sim_scenarios_user_id", "sim_scenarios",
                    ["user_id"], schema="finance")


def downgrade() -> None:
    op.drop_index("ix_finance_sim_scenarios_user_id", table_name="sim_scenarios",
                  schema="finance")
    op.drop_table("sim_scenarios", schema="finance")
