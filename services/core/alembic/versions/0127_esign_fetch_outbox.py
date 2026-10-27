"""서명 완료본 회수 아웃박스 (e-sign #6, 전체검토 2026-06-29 Tier2).

완료 미러(_sign_completed)가 15s 네트워크 GET을 인박스 트랜잭션·행잠금 안에서 수행하던 것을
integration.esign_fetch_outbox로 분리 — 미러는 enqueue만, 회수는 job_esign_fetch가 별도 트랜잭션.
"""
from alembic import op
import sqlalchemy as sa

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "esign_fetch_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.String(24), nullable=False),
        sa.Column("contract_id", sa.BigInteger(), nullable=False),
        sa.Column("doc_url", sa.Text(), nullable=False),
        sa.Column("issue_no", sa.String(64)),
        sa.Column("status", sa.String(10), nullable=False, server_default="PENDING"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("entity_type", "contract_id", name="uq_esign_fetch_outbox"),
        sa.CheckConstraint("status IN ('PENDING','DONE','FAILED','DEAD')",
                           name="ck_esign_fetch_status"),
        schema="integration",
    )
    op.create_index("ix_esign_fetch_outbox_status", "esign_fetch_outbox", ["status"],
                    schema="integration")


def downgrade() -> None:
    op.drop_index("ix_esign_fetch_outbox_status", table_name="esign_fetch_outbox",
                  schema="integration")
    op.drop_table("esign_fetch_outbox", schema="integration")
