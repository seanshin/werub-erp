"""0192 — 재무원장 DB 레벨 불변성(G1).

원장 무결성이 애플리케이션 규율(모든 쓰기가 VoucherService 경유)에만 의존하던 것을
DB 경계에서 강제 — audit.audit_log / scm.narcotics_ledger와 동일 수준으로 격상.

- finance.voucher_lines: REVOKE UPDATE, DELETE FROM erp_app — 라인은 INSERT 전용(GL 금액 데이터,
  코드 전수확인상 UPDATE/DELETE 경로 없음).
- finance.vouchers: BEFORE UPDATE OR DELETE 트리거 —
  · DELETE 항상 차단(정정은 역분개).
  · UPDATE 시 불변 컬럼(voucher_on·source_module·source_ref·created_by·created_at) 변경 차단.
    상태(status)·maker-checker(submitted/approved)·역분개 링크(reversed_by/reverses)·draft_lines만 허용.
  트리거는 모든 DB 역할에 적용되므로 erp_app 상위권한(owner)도 불변 컬럼 변경·전표 삭제 불가.

⚠ 트리거/REVOKE는 PG 전용 — sqlite 테스트 하네스(create_all)엔 미적용(정상 흐름 무영향).
검증: CI가 PG16에서 alembic upgrade/downgrade 왕복 + prod 스모크(불법 update/delete 차단).

Revision ID: 0192
"""
from alembic import op

revision = "0192"
down_revision = "0191"
branch_labels = None
depends_on = None

_FN = """
CREATE OR REPLACE FUNCTION finance.trg_voucher_immutable() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '전표는 삭제할 수 없습니다(불변원장) — 정정은 역분개';
  END IF;
  IF NEW.voucher_on   IS DISTINCT FROM OLD.voucher_on
     OR NEW.source_module IS DISTINCT FROM OLD.source_module
     OR NEW.source_ref    IS DISTINCT FROM OLD.source_ref
     OR NEW.created_by    IS DISTINCT FROM OLD.created_by
     OR NEW.created_at    IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION '전표 불변 컬럼(일자·출처모듈·출처참조·작성자·작성시각)은 변경할 수 없습니다';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(_FN)
    op.execute("""
      CREATE TRIGGER voucher_immutable
      BEFORE UPDATE OR DELETE ON finance.vouchers
      FOR EACH ROW EXECUTE FUNCTION finance.trg_voucher_immutable();
    """)
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
        REVOKE UPDATE, DELETE ON finance.voucher_lines FROM erp_app;
      END IF;
    END $$""")


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
        GRANT UPDATE, DELETE ON finance.voucher_lines TO erp_app;
      END IF;
    END $$""")
    op.execute("DROP TRIGGER IF EXISTS voucher_immutable ON finance.vouchers")
    op.execute("DROP FUNCTION IF EXISTS finance.trg_voucher_immutable()")
