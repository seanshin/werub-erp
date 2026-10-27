"""0057 — 사전심사 적발에 진료일(treated_on) 추가.

날짜특정 적발(중복·일별 산정횟수·동시산정 등)을 진료비 명세의 해당 진료일 라인에만
정확히 부착하기 위해 pre_review_findings에 treated_on 컬럼 추가. 기존 적발은 NULL
(코드 기준 폴백 — 종전 동작 유지). 신규 sync부터 채워짐.

Revision ID: 0057
"""
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE claims.pre_review_findings ADD COLUMN treated_on DATE")


def downgrade() -> None:
    op.execute("ALTER TABLE claims.pre_review_findings DROP COLUMN treated_on")
