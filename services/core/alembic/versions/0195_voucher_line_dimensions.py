"""0195 — 라인 확장 차원(G9 Phase0): voucher_lines.cost_center·project.

라인 차원이 department_id 단일뿐이라 IFRS 8 부문·관리회계 정밀도 제약(G9). department_id와 직교한
관리 차원(코스트센터·프로젝트)을 nullable로 추가. Phase0=컬럼만(무위험·기존 전기 무영향)·
append-only(voucher_lines REVOKE)와 무충돌(INSERT 시에만 설정). 채움·리포팅은 Phase1/2.

Revision ID: 0195
"""
import sqlalchemy as sa
from alembic import op

revision = "0195"
down_revision = "0194"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("voucher_lines", sa.Column("cost_center", sa.String(20), nullable=True),
                  schema="finance")
    op.add_column("voucher_lines", sa.Column("project", sa.String(30), nullable=True),
                  schema="finance")


def downgrade() -> None:
    op.drop_column("voucher_lines", "project", schema="finance")
    op.drop_column("voucher_lines", "cost_center", schema="finance")
