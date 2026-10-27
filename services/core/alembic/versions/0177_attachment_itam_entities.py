"""0177 — 첨부 대상에 ITAM entity_type 허용(잠재 버그 수정·S-9 전제).

common.attachments.ck_attachment_entity 제약이 itam 유형(it_asset·itam_contract·
software_service·itam_maintenance)을 누락 → ITAM 사진·계약서·소거확인서 업로드가
CHECK 위반으로 전부 실패하던 잠재 버그. 유형을 제약에 추가한다.

Revision ID: 0177
"""
from alembic import op

revision = "0177"
down_revision = "0176"
branch_labels = None
depends_on = None

_OLD = ("tax_invoice", "voucher", "po_receipt", "contract", "employee", "other")
_ITAM = ("it_asset", "itam_contract", "software_service", "itam_maintenance")


def _recreate(entity_types: tuple[str, ...]) -> None:
    op.drop_constraint("ck_attachment_entity", "attachments", schema="common")
    vals = ", ".join(f"'{e}'" for e in entity_types)
    op.create_check_constraint("ck_attachment_entity", "attachments",
                               f"entity_type IN ({vals})", schema="common")


def upgrade() -> None:
    _recreate(_OLD + _ITAM)


def downgrade() -> None:
    _recreate(_OLD)
