"""0059 — 사전심사 케이스 자동 이상없음(P1) 표시 컬럼.

무적발(리스크0) 케이스를 자동 CLEARED 처리할 때, 자동/수동 종결을 구분하고
가역·감사 가능하도록 auto_cleared 플래그 추가. 기본 false(기존 케이스 영향 없음).

Revision ID: 0059
"""
from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE claims.pre_review_cases "
               "ADD COLUMN auto_cleared BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE claims.pre_review_cases DROP COLUMN auto_cleared")
