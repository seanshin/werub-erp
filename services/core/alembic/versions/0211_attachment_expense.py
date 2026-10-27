"""0211 — 첨부 CHECK 제약에 'expense' 추가 (v1.175 핫픽스).

v1.175(0210)가 경비 영수증 첨부(entity_type='expense')를 _ENTITY_SCOPE에 등록했으나
ck_attachment_entity 허용 목록 갱신을 누락 — 경비 영수증 업로드가 IntegrityError로 실패하던
실결함(기안문서 기획 적대검토 H-2에서 적발·prod 제약 실측 확인).

Revision ID: 0211
"""
from alembic import op

revision = "0211"
down_revision = "0210"
branch_labels = None
depends_on = None

_TYPES_OLD = ("'tax_invoice','voucher','po_receipt','contract','employee','other',"
              "'it_asset','itam_contract','software_service','itam_maintenance',"
              "'itam_offboarding','org'")
_TYPES_NEW = _TYPES_OLD + ",'expense'"


def upgrade() -> None:
    op.drop_constraint("ck_attachment_entity", "attachments", schema="common")
    op.create_check_constraint("ck_attachment_entity", "attachments",
                               f"entity_type IN ({_TYPES_NEW})", schema="common")


def downgrade() -> None:
    op.execute("DELETE FROM common.attachments WHERE entity_type = 'expense'")
    op.drop_constraint("ck_attachment_entity", "attachments", schema="common")
    op.create_check_constraint("ck_attachment_entity", "attachments",
                               f"entity_type IN ({_TYPES_OLD})", schema="common")
