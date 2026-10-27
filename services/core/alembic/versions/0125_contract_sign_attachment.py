"""계약 서명 완료본 Attachment 링크 (e-sign #6 §2) — 회수한 완료본 PDF 참조.

sign.completed 수신 시 완료본을 ERP Attachment(sha256)로 회수하고 그 id를 계약에 링크.
nullable — 미회수/미서명 계약 무영향. 참조: erp-reply-his-open-requests-2026-06-28 §A④.
"""
import sqlalchemy as sa
from alembic import op

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for tbl, sch in (("medical_contracts", "hr"), ("outsourcing_contracts", "scm")):
        op.add_column(tbl, sa.Column("sign_doc_attachment_id", sa.BigInteger(), nullable=True),
                      schema=sch)


def downgrade() -> None:
    for tbl, sch in (("medical_contracts", "hr"), ("outsourcing_contracts", "scm")):
        op.drop_column(tbl, "sign_doc_attachment_id", schema=sch)
