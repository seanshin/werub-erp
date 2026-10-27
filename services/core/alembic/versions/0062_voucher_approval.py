"""0062 — 전표 승인 워크플로(M2 maker-checker).

수동 전표 DRAFT→PENDING→POSTED(승인)|REJECTED. DRAFT/PENDING은 원장 라인 미생성
(draft_lines 보관) → 승인 시 전기. 자동전표는 기존대로 즉시 POSTED.
status CHECK 확장 + 승인 메타 컬럼 + draft_lines.

Revision ID: 0062
"""
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

# prod 실제 제약명 분기(0061 교훈) — 모델 name(ck_voucher_status)·PG 자동명 둘 다 IF EXISTS.
_CK_MODEL = "ck_voucher_status"
_CK_AUTO = "vouchers_status_check"


def upgrade() -> None:
    for col, typ in (("submitted_by", "BIGINT"), ("submitted_at", "TIMESTAMPTZ"),
                     ("approved_by", "BIGINT"), ("approved_at", "TIMESTAMPTZ"),
                     ("reject_reason", "TEXT"), ("draft_lines", "JSONB")):
        op.execute(f"ALTER TABLE finance.vouchers ADD COLUMN IF NOT EXISTS {col} {typ}")
    op.execute(f"ALTER TABLE finance.vouchers DROP CONSTRAINT IF EXISTS {_CK_MODEL}")
    op.execute(f"ALTER TABLE finance.vouchers DROP CONSTRAINT IF EXISTS {_CK_AUTO}")
    op.execute(f"ALTER TABLE finance.vouchers ADD CONSTRAINT {_CK_MODEL} "
               "CHECK (status IN ('DRAFT','PENDING','POSTED','REJECTED','REVERSED'))")


def downgrade() -> None:
    op.execute(f"ALTER TABLE finance.vouchers DROP CONSTRAINT IF EXISTS {_CK_MODEL}")
    op.execute(f"ALTER TABLE finance.vouchers ADD CONSTRAINT {_CK_MODEL} "
               "CHECK (status IN ('POSTED','REVERSED'))")
    for col in ("draft_lines", "reject_reason", "approved_at", "approved_by",
                "submitted_at", "submitted_by"):
        op.execute(f"ALTER TABLE finance.vouchers DROP COLUMN IF EXISTS {col}")
