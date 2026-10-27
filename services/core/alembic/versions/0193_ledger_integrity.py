"""0193 — 원장 무결성 체크포인트 테이블(G11·해시 체인·append-only).

voucher_lines(append-only) 증분 세그먼트를 해시 체인화해 트리거/REVOKE를 우회하는 superuser
변조까지 사후 탐지. 테이블 자체도 append-only(REVOKE UPDATE/DELETE) — 체크포인트 위변조 방지.

Revision ID: 0193
"""
import sqlalchemy as sa
from alembic import op

revision = "0193"
down_revision = "0192"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ledger_integrity",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("up_to_line_id", sa.BigInteger(), nullable=False),
        sa.Column("line_count", sa.BigInteger(), nullable=False),
        sa.Column("sum_debit", sa.Numeric(18, 0), nullable=False),
        sa.Column("sum_credit", sa.Numeric(18, 0), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        schema="finance",
    )
    # append-only 강제 — 체크포인트는 삽입만(위변조 방지·audit_log 패턴)
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
        GRANT SELECT, INSERT ON finance.ledger_integrity TO erp_app;
        REVOKE UPDATE, DELETE ON finance.ledger_integrity FROM erp_app;
      END IF;
    END $$""")


def downgrade() -> None:
    op.drop_table("ledger_integrity", schema="finance")
