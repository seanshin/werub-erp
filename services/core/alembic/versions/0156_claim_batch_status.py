"""claims.claim_batches 지급 생애주기 상태(status) — 미지급 청구 조회·진행 추적.

ClaimBatch가 심사결과 통보(MEDRLT)에서 생성되므로 ERP 추적 신호는 심사완료→(부분)지급→지급완료.
지급 대사(0155)와 연동해 진료월 결정액 대비 지급액으로 배치 상태를 갱신 → "미지급 청구 조회" 완성.
"""
import sqlalchemy as sa
from alembic import op

revision = "0156"
down_revision = "0155"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("claim_batches",
                  sa.Column("status", sa.String(length=16), nullable=False,
                            server_default="DECIDED"),
                  schema="claims")
    op.create_index("ix_claim_batches_status", "claim_batches", ["status"], schema="claims")


def downgrade() -> None:
    op.drop_index("ix_claim_batches_status", table_name="claim_batches", schema="claims")
    op.drop_column("claim_batches", "status", schema="claims")
