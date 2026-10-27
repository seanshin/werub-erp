"""0060 — 적발 자동 오탐(P2) 표시 컬럼.

확정 오탐 패턴(룰×코드 전건 인간 오탐) 자동 DISMISS 시, 인간 결정과 구분하고
캘리브레이션 집계에서 제외(피드백 루프 방지)·가역·감사 가능하도록 auto_decided 추가.
기본 false(기존 적발 영향 없음).

Revision ID: 0060
"""
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE claims.pre_review_findings "
               "ADD COLUMN auto_decided BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE claims.pre_review_findings DROP COLUMN auto_decided")
