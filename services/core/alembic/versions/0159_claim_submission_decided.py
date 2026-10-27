"""claims.claim_submissions 결정 마커(decided_on/decided_amt) — 변동대가 미결정 풀 재정의.

변동대가(K-IFRS 1115) 미결정 풀을 ClaimStatement(=이미 결정) 프록시에서 **제출(ClaimSubmission)−
결정**으로 재정의한다. 결정 신호를 실제 삭감(claim.reviewed, 5910)과 일치시켜 제출에 decided 마킹 →
추정 풀과 실적 타이밍 정렬로 순이익 이중차감 제거. decided_on 기준 as-of 재구성으로 마감월 재실행 오조정 해소.
"""
import sqlalchemy as sa
from alembic import op

revision = "0159"
down_revision = "0158"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("claim_submissions", sa.Column("decided_on", sa.Date(), nullable=True),
                  schema="claims")
    op.add_column("claim_submissions", sa.Column("decided_amt", sa.Numeric(14, 0), nullable=True),
                  schema="claims")


def downgrade() -> None:
    op.drop_column("claim_submissions", "decided_amt", schema="claims")
    op.drop_column("claim_submissions", "decided_on", schema="claims")
