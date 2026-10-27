"""0049 — 심사 케이스 입원/외래 구분(encounter_type).

라인(BillingLine)에는 있던 정보를 케이스로 승격 — 입원/외래는 본인부담률·심사 룰이
달라 심사팀 1차 분류 기준. 한 차트·월에 혼재 시 MIXED.

Revision ID: 0049
"""
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE claims.pre_review_cases "
               "ADD COLUMN encounter_type VARCHAR(12)")


def downgrade() -> None:
    op.execute("ALTER TABLE claims.pre_review_cases DROP COLUMN encounter_type")
