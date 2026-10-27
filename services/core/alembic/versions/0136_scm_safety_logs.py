"""의약품 회수 + 의료폐기물 배출대장 (규제·안전 logging — 법정).

drug_recalls(식약처 회수명령 대응·영향 로트 추적)·medical_waste_logs(폐기물관리법 배출/인계 대장).
"""
from alembic import op
import sqlalchemy as sa

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "drug_recalls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.BigInteger(), sa.ForeignKey("scm.items.id"), nullable=False),
        sa.Column("lot_no", sa.String(40)),                 # null=전 로트
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False, server_default="VOLUNTARY"),
        sa.Column("recall_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="OPEN"),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("severity IN ('VOLUNTARY','CLASS1','CLASS2','CLASS3')",
                           name="ck_recall_severity"),
        sa.CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_recall_status"),
        schema="scm",
    )
    op.create_index("ix_drug_recalls_item", "drug_recalls", ["item_id"], schema="scm")
    op.create_table(
        "medical_waste_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("waste_type", sa.String(20), nullable=False),   # 격리·위해·일반·손상성·병리계 등
        sa.Column("weight_kg", sa.Numeric(10, 2), nullable=False),
        sa.Column("stored_on", sa.Date(), nullable=False),        # 보관 시작
        sa.Column("handed_over_on", sa.Date()),                   # 처리업체 인계일
        sa.Column("handler", sa.String(120)),                     # 처리업체
        sa.Column("manifest_no", sa.String(60)),                  # 올바로 인계서 번호
        sa.Column("status", sa.String(10), nullable=False, server_default="STORED"),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('STORED','HANDED_OVER')", name="ck_waste_status"),
        schema="scm",
    )
    op.create_index("ix_waste_stored", "medical_waste_logs", ["stored_on"], schema="scm")


def downgrade() -> None:
    op.drop_index("ix_waste_stored", table_name="medical_waste_logs", schema="scm")
    op.drop_table("medical_waste_logs", schema="scm")
    op.drop_index("ix_drug_recalls_item", table_name="drug_recalls", schema="scm")
    op.drop_table("drug_recalls", schema="scm")
