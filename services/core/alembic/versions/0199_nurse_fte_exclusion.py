"""0199 — 간호등급 산정 정밀화: hr.employees 주 근로시간(FTE) + 산정 제외 사유.

part_time_fte 환산표(고시 다.3)는 구현돼 있으나 입력 필드가 없어 전원 전일제 1.0 가정이던 것 해소.
간호감독·노조전임·호스피스 등 산정 제외 대상(고시)도 사유 필드로 명시적 제외.

Revision ID: 0199
"""
import sqlalchemy as sa
from alembic import op

revision = "0199"
down_revision = "0198"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("weekly_hours", sa.Numeric(4, 1), nullable=True),
                  schema="hr")
    op.add_column("employees", sa.Column("nursing_exclude_reason", sa.String(20), nullable=True),
                  schema="hr")


def downgrade() -> None:
    op.drop_column("employees", "nursing_exclude_reason", schema="hr")
    op.drop_column("employees", "weekly_hours", schema="hr")
