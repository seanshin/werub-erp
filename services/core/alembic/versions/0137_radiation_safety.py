"""방사선 안전관리 (진단용방사선 안전관리규칙) — 종사자 피폭선량·장비 정기검사 (규제 심화).

radiation_doses(분기 피폭선량)·radiation_equipment(장비 정기검사일·만기 추적).
"""
from alembic import op
import sqlalchemy as sa

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "radiation_doses",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),       # yyyy-Qn 또는 yyyy-mm
        sa.Column("dose_msv", sa.Numeric(7, 3), nullable=False),  # 측정 피폭선량(mSv)
        sa.Column("instrument", sa.String(60)),                   # 측정기관·선량계
        sa.Column("measured_on", sa.Date(), nullable=False),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("employee_id", "period", name="uq_rad_dose"),
        schema="regulatory",
    )
    op.create_index("ix_rad_dose_emp", "radiation_doses", ["employee_id"], schema="regulatory")
    op.create_table(
        "radiation_equipment",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("equipment_no", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("last_inspected_on", sa.Date()),
        sa.Column("next_inspection_on", sa.Date(), nullable=False),   # 정기검사 만기
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="regulatory",
    )
    op.create_index("ix_rad_equip_next", "radiation_equipment", ["next_inspection_on"],
                    schema="regulatory")


def downgrade() -> None:
    op.drop_index("ix_rad_equip_next", table_name="radiation_equipment", schema="regulatory")
    op.drop_table("radiation_equipment", schema="regulatory")
    op.drop_index("ix_rad_dose_emp", table_name="radiation_doses", schema="regulatory")
    op.drop_table("radiation_doses", schema="regulatory")
