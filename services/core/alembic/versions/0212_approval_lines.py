"""0212 — 결재선 요청·관리(기안문서 선행 기반): 템플릿 + 요청 워크플로.

사용자 결정(2026-07-21): R-1 ADMIN 겸임·R-2 템플릿 선택만·R-5 기존 ApprovalRule과 공존.
- common.approval_line_templates: 명명 결재선(ORG 공용/PERSONAL 개인) — 상신 시 선택 단위.
- common.approval_line_requests: 표준 결재선 신설/변경/폐기 요청 → 관리자 검토 → 프로비저닝.
결재 인스턴스는 기존 approval_instances/steps 재사용(신규 없음).

Revision ID: 0212
"""
import sqlalchemy as sa
from alembic import op

revision = "0212"
down_revision = "0211"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_line_templates",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("scope", sa.String(10), nullable=False, server_default="PERSONAL", index=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=True, index=True),
        sa.Column("department_id", sa.BigInteger(), nullable=True),
        sa.Column("doc_types", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("steps", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_request_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("scope IN ('ORG','PERSONAL')", name="ck_apl_scope"),
        schema="common",
    )
    op.create_table(
        "approval_line_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("req_no", sa.String(24), nullable=False),
        sa.Column("request_type", sa.String(10), nullable=False, server_default="NEW"),
        sa.Column("target_template_id", sa.BigInteger(), nullable=True),
        sa.Column("proposed_name", sa.String(120), nullable=True),
        sa.Column("proposed_steps", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("doc_types", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("reason", sa.String(400), nullable=True),
        sa.Column("department_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="REQUESTED", index=True),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(400), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("req_no", name="uq_aplr_reqno"),
        sa.CheckConstraint("request_type IN ('NEW','MODIFY','RETIRE')", name="ck_aplr_type"),
        sa.CheckConstraint("status IN ('REQUESTED','APPROVED','REJECTED','CANCELED')",
                           name="ck_aplr_status"),
        schema="common",
    )


def downgrade() -> None:
    op.drop_table("approval_line_requests", schema="common")
    op.drop_table("approval_line_templates", schema="common")
