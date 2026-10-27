"""claims.appeals 알림 단계(alert_stage) — 기한임박 알림 무손실화(멱등 발송).

deadline_alert_targets가 legal_deadline == today+d 정확일치라 배치 1일 누락(휴일·장애) 시 그날
D-d 걸리는 이의신청이 어떤 알림도 못 받고 영구 소실되던 결함(claims 심층 재검토). alert_stage에
마지막 발송 임계(가장 작은 D-버킷)를 기록 → 임계 이하(≤) 판정으로 누락 없이, 버킷당 1회만 발송.
"""
import sqlalchemy as sa
from alembic import op

revision = "0154"
down_revision = "0153"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("appeals", sa.Column("alert_stage", sa.Integer(), nullable=True),
                  schema="claims")


def downgrade() -> None:
    op.drop_column("appeals", "alert_stage", schema="claims")
