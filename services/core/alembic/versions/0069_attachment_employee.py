"""0069 — 첨부 entity_type에 employee 추가(직원·의료진 계약 서류 관리).

기존 contract는 의료진 계약 서류로 활용. employee는 직원 일반 서류.

Revision ID: 0069
"""
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None

_CK = "ck_attachment_entity"
_AUTO = "attachments_entity_type_check"
_NEW = "entity_type IN ('tax_invoice','voucher','po_receipt','contract','employee','other')"
_OLD = "entity_type IN ('tax_invoice','voucher','po_receipt','contract','other')"


def upgrade() -> None:
    op.execute(f"ALTER TABLE common.attachments DROP CONSTRAINT IF EXISTS {_CK}")
    op.execute(f"ALTER TABLE common.attachments DROP CONSTRAINT IF EXISTS {_AUTO}")
    op.execute(f"ALTER TABLE common.attachments ADD CONSTRAINT {_CK} CHECK ({_NEW})")


def downgrade() -> None:
    op.execute(f"ALTER TABLE common.attachments DROP CONSTRAINT IF EXISTS {_CK}")
    op.execute(f"ALTER TABLE common.attachments ADD CONSTRAINT {_CK} CHECK ({_OLD})")
