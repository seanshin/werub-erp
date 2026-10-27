"""0184 — 기관 설정 확대(도메인·커스텀) + 첨부 org(로고·이미지).

org_settings에 domain(도메인 주소)·extra(기타 커스텀 [{key,value}]) 추가.
첨부 entity_type에 org 추가(로고·기타 이미지). (요청·#6)

Revision ID: 0184
"""
import sqlalchemy as sa
from alembic import op

revision = "0184"
down_revision = "0183"
branch_labels = None
depends_on = None

_BASE = ("tax_invoice", "voucher", "po_receipt", "contract", "employee", "other",
         "it_asset", "itam_contract", "software_service", "itam_maintenance", "itam_offboarding")


def _recreate(entity_types: tuple[str, ...]) -> None:
    op.drop_constraint("ck_attachment_entity", "attachments", schema="common")
    vals = ", ".join(f"'{e}'" for e in entity_types)
    op.create_check_constraint("ck_attachment_entity", "attachments",
                               f"entity_type IN ({vals})", schema="common")


def upgrade() -> None:
    op.add_column("org_settings", sa.Column("domain", sa.String(200)), schema="common")
    op.add_column("org_settings", sa.Column("extra", sa.JSON), schema="common")
    _recreate(_BASE + ("org",))


def downgrade() -> None:
    _recreate(_BASE)
    op.drop_column("org_settings", "extra", schema="common")
    op.drop_column("org_settings", "domain", schema="common")
