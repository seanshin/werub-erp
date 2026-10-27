"""0203 — 결재 불변 원장(블록체인식 해시체인): common.approval_ledger + append-only REVOKE.

결재 행위(상신·승인·반려·전권)를 row_hash=SHA-256(prev‖필드) 전역 체인으로 기록. erp_app
UPDATE/DELETE REVOKE로 DB 계층 append-only(voucher_lines 0192·narcotics 동일 패턴) — verify가
전 구간 재계산 대조해 superuser 변조까지 사후 탐지.

⚠ append-only 테이블 — 마이그에서 UPDATE/백필 금지(0122 교훈). 신규 테이블이라 백필 없음.

Revision ID: 0203
"""
import sqlalchemy as sa
from alembic import op

revision = "0203"
down_revision = "0202"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("seq_no", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("doc_type", sa.String(24), nullable=False, index=True),
        sa.Column("doc_id", sa.BigInteger(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("actor", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("row_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("seq_no", name="uq_approval_ledger_seq"),
        schema="common",
    )
    # DB 계층 append-only — 코드 전수확인상 UPDATE/DELETE 경로 없음(체인 불변식)
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
        REVOKE UPDATE, DELETE ON common.approval_ledger FROM erp_app;
      END IF;
    END $$""")


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
        GRANT UPDATE, DELETE ON common.approval_ledger TO erp_app;
      END IF;
    END $$""")
    op.drop_table("approval_ledger", schema="common")
