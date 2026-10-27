"""0225 — 감사로그 DB 레벨 불변성 격상 (INV-AUDIT-01 정렬·배치중립 Phase0 감사 G1).

audit.audit_log/access_log 불변성이 0001의 REVOKE(erp_app 한정·롤 부재 시 무음 스킵)에만
의존하던 것을 0192(재무원장)와 동일한 트리거로 격상 — owner/superuser 포함 전 역할 차단.

⚠ 트리거는 PG 전용 — sqlite 테스트 하네스엔 미적용(정상 흐름 무영향). 파기(보존연한)는
관리자 승인 후 트리거 일시 해제 절차로만(retention_service 주석과 정합).

Revision ID: 0225
"""
from alembic import op

revision = "0225"
down_revision = "0224"
branch_labels = None
depends_on = None

_FN = """
CREATE OR REPLACE FUNCTION audit.trg_audit_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '감사로그는 수정/삭제할 수 없습니다(불변·append-only)';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(_FN)
    for tbl in ("audit_log", "access_log"):
        op.execute(f"""
          CREATE TRIGGER {tbl}_immutable
          BEFORE UPDATE OR DELETE ON audit.{tbl}
          FOR EACH ROW EXECUTE FUNCTION audit.trg_audit_immutable();
        """)
    # 0001의 조건부 REVOKE(롤 부재 시 무음 스킵) 재적용 — 존재 시 멱등
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
        REVOKE UPDATE, DELETE ON audit.audit_log, audit.access_log FROM erp_app;
      END IF;
    END $$""")


def downgrade() -> None:
    for tbl in ("audit_log", "access_log"):
        op.execute(f"DROP TRIGGER IF EXISTS {tbl}_immutable ON audit.{tbl}")
    op.execute("DROP FUNCTION IF EXISTS audit.trg_audit_immutable()")
