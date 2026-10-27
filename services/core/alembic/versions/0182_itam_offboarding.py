"""0182 — IT 퇴사 회수 기록(offboardings) + 첨부 itam_offboarding.

퇴사 회수를 기록으로 관리(검토자·인수인계자·비고·실물 사진). 첨부 entity_type에 itam_offboarding 추가.

Revision ID: 0182
"""
import sqlalchemy as sa
from alembic import op

revision = "0182"
down_revision = "0181"
branch_labels = None
depends_on = None

_BASE = ("tax_invoice", "voucher", "po_receipt", "contract", "employee", "other",
         "it_asset", "itam_contract", "software_service", "itam_maintenance")


def _recreate(entity_types: tuple[str, ...]) -> None:
    op.drop_constraint("ck_attachment_entity", "attachments", schema="common")
    vals = ", ".join(f"'{e}'" for e in entity_types)
    op.create_check_constraint("ck_attachment_entity", "attachments",
                               f"entity_type IN ({vals})", schema="common")


def upgrade() -> None:
    op.create_table(
        "offboardings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.BigInteger, sa.ForeignKey("hr.employees.id"),
                  nullable=False, index=True),
        sa.Column("reviewer_id", sa.BigInteger),        # 검토자
        sa.Column("handover_to_id", sa.BigInteger),     # 인수인계자
        sa.Column("status", sa.String(12), nullable=False, server_default="IN_PROGRESS"),
        sa.Column("note", sa.Text),
        sa.Column("completed_on", sa.Date),
        sa.Column("completed_by", sa.BigInteger),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="itam",
    )
    _recreate(_BASE + ("itam_offboarding",))


def downgrade() -> None:
    _recreate(_BASE)
    op.drop_table("offboardings", schema="itam")
