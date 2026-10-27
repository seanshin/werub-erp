"""0213 — 일반 전자결재(기안문서): general_drafts·draft_viewers + 적대검토 반영.

기획 general-eapproval-drafts.plan.md §10~11. 결재선 요청·관리(0212) 위에서 상신 시 템플릿 선택 소비.
적대검토 H-3: 회수 시 결재 인스턴스를 CANCELED로 종결해야 하므로 ck_appinst_status에 CANCELED 추가.
M-2: 기안 첨부(entity_type='draft') → ck_attachment_entity 확장.

Revision ID: 0213
"""
import sqlalchemy as sa
from alembic import op

revision = "0213"
down_revision = "0212"
branch_labels = None
depends_on = None

_ATT_OLD = ("'tax_invoice','voucher','po_receipt','contract','employee','other',"
            "'it_asset','itam_contract','software_service','itam_maintenance',"
            "'itam_offboarding','org','expense'")
_ATT_NEW = _ATT_OLD + ",'draft'"


def upgrade() -> None:
    op.create_table(
        "general_drafts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("doc_no", sa.String(24), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("category", sa.String(24), nullable=False, server_default="GENERAL"),
        sa.Column("department_id", sa.BigInteger(), nullable=True),
        sa.Column("ref_amount", sa.Numeric(16, 0), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="DRAFT", index=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False, index=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("doc_no", name="uq_gdraft_docno"),
        sa.CheckConstraint("status IN ('DRAFT','PENDING','APPROVED','REJECTED','CANCELED')",
                           name="ck_gdraft_status"),
        schema="common",
    )
    op.create_table(
        "draft_viewers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("draft_id", sa.BigInteger(),
                  sa.ForeignKey("common.general_drafts.id"), nullable=False, index=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("draft_id", "user_id", name="uq_draft_viewer"),
        schema="common",
    )
    # H-3 — 회수 시 결재 인스턴스 CANCELED 종결(좀비 워크리스트·취소문서 APPROVE 방지)
    op.drop_constraint("ck_appinst_status", "approval_instances", schema="common", type_="check")
    op.create_check_constraint(
        "ck_appinst_status", "approval_instances",
        "status IN ('PENDING','APPROVED','REJECTED','CANCELED')", schema="common")
    # M-2 — 기안 첨부
    op.drop_constraint("ck_attachment_entity", "attachments", schema="common")
    op.create_check_constraint("ck_attachment_entity", "attachments",
                               f"entity_type IN ({_ATT_NEW})", schema="common")


def downgrade() -> None:
    op.drop_constraint("ck_attachment_entity", "attachments", schema="common")
    op.execute("DELETE FROM common.attachments WHERE entity_type = 'draft'")
    op.create_check_constraint("ck_attachment_entity", "attachments",
                               f"entity_type IN ({_ATT_OLD})", schema="common")
    op.execute("UPDATE common.approval_instances SET status='REJECTED' WHERE status='CANCELED'")
    op.drop_constraint("ck_appinst_status", "approval_instances", schema="common", type_="check")
    op.create_check_constraint(
        "ck_appinst_status", "approval_instances",
        "status IN ('PENDING','APPROVED','REJECTED')", schema="common")
    op.drop_table("draft_viewers", schema="common")
    op.drop_table("general_drafts", schema="common")
