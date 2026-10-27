"""0232 — 인사평가 등급 → 성과급 연계(옵트인·보완 기획서 P1-1).

인사평가(evaluation) 확정 등급(final_grade)을 성과급(incentive) 산정에 배율로 반영. 기존 성과급을
조용히 바꾸지 않도록 payroll_settings.incentive_grade_link 플래그로 옵트인(기본 OFF=현행 동일).

- payroll_settings.incentive_grade_link: 등급 배율 적용 여부(bool).
- 배율 맵은 코드 상수(S=1.2/A=1.1/B=1.0/C=0.9/D=0.8). 등급 없으면 1.0(무영향).

Revision ID: 0232
"""
import sqlalchemy as sa
from alembic import op

revision = "0232"
down_revision = "0231"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payroll_settings",
                  sa.Column("incentive_grade_link", sa.Boolean(), nullable=False,
                            server_default=sa.text("false")), schema="hr")


def downgrade() -> None:
    op.drop_column("payroll_settings", "incentive_grade_link", schema="hr")
