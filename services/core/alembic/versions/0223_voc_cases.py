"""0223 — 환자 민원·CS(VOC) 관리 (내부운영 갭 감사 Tier3 ①).

접수→분류→배정→처리→회신→종결 생애주기 + 처리 이력. 의료기관 인증평가(민원·고충처리 체계) 소스.
voc_cases(사건)·voc_actions(이력). 연락처는 AES-GCM(contact_enc·환자 연결 시 미저장).

Revision ID: 0223
"""
import sqlalchemy as sa
from alembic import op

revision = "0223"
down_revision = "0222"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voc_cases",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("case_no", sa.String(24), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False, server_default="DESK"),
        sa.Column("vtype", sa.String(12), nullable=False, server_default="COMPLAINT"),
        sa.Column("category", sa.String(10), nullable=False, server_default="ETC"),
        sa.Column("severity", sa.String(8), nullable=False, server_default="LOW"),
        sa.Column("patient_id", sa.BigInteger(), sa.ForeignKey("patient.patients.id"),
                  nullable=True, index=True),
        sa.Column("contact_enc", sa.LargeBinary(), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("department_id", sa.BigInteger(), nullable=True, index=True),
        sa.Column("assignee_user_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="RECEIVED", index=True),
        sa.Column("received_on", sa.Date(), nullable=False, index=True),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer_body", sa.Text(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopen_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safety_incident_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("case_no", name="uq_voc_case_no"),
        sa.CheckConstraint("channel IN ('DESK','PHONE','LETTER','WEB','ETC')",
                           name="ck_voc_channel"),
        sa.CheckConstraint("vtype IN ('COMPLAINT','GRIEVANCE','SUGGESTION','PRAISE','INQUIRY')",
                           name="ck_voc_vtype"),
        sa.CheckConstraint(
            "category IN ('CARE','ADMIN','FACILITY','STAFF','COST','PRIVACY','ETC')",
            name="ck_voc_category"),
        sa.CheckConstraint("severity IN ('LOW','MEDIUM','HIGH')", name="ck_voc_severity"),
        sa.CheckConstraint("status IN ('RECEIVED','IN_PROGRESS','ANSWERED','CLOSED')",
                           name="ck_voc_status"),
        schema="patient",
    )
    op.create_table(
        "voc_actions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("case_id", sa.BigInteger(), sa.ForeignKey("patient.voc_cases.id"),
                  nullable=False, index=True),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("actor", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("action IN ('ASSIGN','COMMENT','ANSWER','CLOSE','REOPEN')",
                           name="ck_voca_action"),
        schema="patient",
    )


def downgrade() -> None:
    op.drop_table("voc_actions", schema="patient")
    op.drop_table("voc_cases", schema="patient")
