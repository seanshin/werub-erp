"""0204 — 결재 원장 하드닝: 앵커 체크포인트 + immutability 트리거(owner 차단).

적대검토 대응:
- HIGH-2 꼬리 절단: common.approval_ledger_anchor(검증된 head 봉인) — 최근 행 삭제 탐지.
- MEDIUM 트리거 백스톱: approval_ledger·anchor에 BEFORE UPDATE OR DELETE RAISE 트리거.
  0203의 REVOKE는 소유자(erp_app)가 스스로 GRANT 복원 가능 → 트리거는 owner에도 발화(0192 vouchers
  동형)라 append-only를 DB 계층에서 강제. 정정은 역행위 append(원장 삭제·수정 불허).
- HIGH-1(무키→HMAC)은 코드(서명키) 전환이라 스키마 변경 없음. 기존 SHA-256 행이 있으면 verify가
  실패하나, prod approval_ledger 0행 확인(전환 안전).

⚠ append-only — 마이그 UPDATE/백필 금지(0122 교훈). 신규 테이블·백필 없음.

Revision ID: 0204
"""
import sqlalchemy as sa
from alembic import op

revision = "0204"
down_revision = "0203"
branch_labels = None
depends_on = None

_FN = """
CREATE OR REPLACE FUNCTION common.trg_approval_ledger_immutable()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION '결재 원장은 append-only입니다 (UPDATE/DELETE 불가·정정은 역행위 기록)';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "approval_ledger_anchor",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("seq_no", sa.BigInteger(), nullable=False),
        sa.Column("entry_count", sa.BigInteger(), nullable=False),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("mac", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        schema="common",
    )
    # immutability 트리거 — owner(erp_app)에도 발화해 UPDATE/DELETE 전면 차단(REVOKE 백스톱)
    op.execute(_FN)
    for tbl in ("approval_ledger", "approval_ledger_anchor"):
        op.execute(f"""
          CREATE TRIGGER {tbl}_immutable
          BEFORE UPDATE OR DELETE ON common.{tbl}
          FOR EACH ROW EXECUTE FUNCTION common.trg_approval_ledger_immutable();
        """)
        op.execute(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
            REVOKE UPDATE, DELETE ON common.{tbl} FROM erp_app;
          END IF;
        END $$""")


def downgrade() -> None:
    for tbl in ("approval_ledger", "approval_ledger_anchor"):
        op.execute(f"DROP TRIGGER IF EXISTS {tbl}_immutable ON common.{tbl}")
        op.execute(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
            GRANT UPDATE, DELETE ON common.{tbl} TO erp_app;
          END IF;
        END $$""")
    op.execute("DROP FUNCTION IF EXISTS common.trg_approval_ledger_immutable()")
    op.drop_table("approval_ledger_anchor", schema="common")
