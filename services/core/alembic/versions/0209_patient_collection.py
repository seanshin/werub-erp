"""0209 — 환자 미수금 회수 루프: 독촉·분납·개별 대손상각 (내부운영 갭 감사 원무 ①).

발생주의·aging·ECL 충당은 구축돼 있으나 회수 워크플로(독촉→분납→개별상각)가 비어 미수 회수 루프가
끊기던 갭 보완:
- finance.receivables: status에 WRITTEN_OFF 추가 + written_off_on/write_off_reason(개별 상각).
- patient.dunning_notices: 독촉(납부안내) 이력 — 차수 자동 증가.
- patient.installment_plans: 분납 합의 — 채권당 ACTIVE 1건(부분 유니크).

Revision ID: 0209
"""
import sqlalchemy as sa
from alembic import op

revision = "0209"
down_revision = "0208"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) receivables — WRITTEN_OFF 상태 + 상각 컬럼.
    # 0009가 인라인 무명 CHECK로 생성 → PG 자동명 receivables_status_check(모델명 ck_ar_status 아님).
    # 양쪽 IF EXISTS 드롭 후 모델과 같은 이름(ck_ar_status)으로 재생성(이후 마이그 일관).
    op.execute("ALTER TABLE finance.receivables DROP CONSTRAINT IF EXISTS receivables_status_check")
    op.execute("ALTER TABLE finance.receivables DROP CONSTRAINT IF EXISTS ck_ar_status")
    op.create_check_constraint(
        "ck_ar_status", "receivables",
        "status IN ('OUTSTANDING','PARTIAL','COLLECTED','WRITTEN_OFF')", schema="finance")
    op.add_column("receivables", sa.Column("written_off_on", sa.Date(), nullable=True),
                  schema="finance")
    op.add_column("receivables", sa.Column("write_off_reason", sa.String(200), nullable=True),
                  schema="finance")

    # 2) 독촉 이력
    op.create_table(
        "dunning_notices",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("receivable_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("chart_no", sa.String(20), nullable=False, index=True),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("method", sa.String(10), nullable=False, server_default="PHONE"),
        sa.Column("outstanding", sa.Numeric(14, 0), nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("sent_on", sa.Date(), nullable=False, index=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("method IN ('SMS','MAIL','PHONE','VISIT')", name="ck_dunning_method"),
        schema="patient",
    )

    # 3) 분납 합의 — 채권당 ACTIVE 1건
    op.create_table(
        "installment_plans",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("receivable_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("chart_no", sa.String(20), nullable=False, index=True),
        sa.Column("total_amount", sa.Numeric(14, 0), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("monthly_amount", sa.Numeric(14, 0), nullable=False),
        sa.Column("start_month", sa.String(7), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="ACTIVE"),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE','COMPLETED','DEFAULTED','CANCELED')",
                           name="ck_installment_status"),
        schema="patient",
    )
    op.create_index("uq_installment_active", "installment_plans", ["receivable_id"],
                    unique=True, schema="patient",
                    postgresql_where=sa.text("status = 'ACTIVE'"))


def downgrade() -> None:
    op.drop_index("uq_installment_active", "installment_plans", schema="patient")
    op.drop_table("installment_plans", schema="patient")
    op.drop_table("dunning_notices", schema="patient")
    op.drop_column("receivables", "write_off_reason", schema="finance")
    op.drop_column("receivables", "written_off_on", schema="finance")
    op.execute("ALTER TABLE finance.receivables DROP CONSTRAINT IF EXISTS ck_ar_status")
    # 남은 WRITTEN_OFF 행이 있으면 구 CHECK 재생성이 실패하므로 OUTSTANDING으로 되돌림(다운그레이드 안전)
    op.execute("UPDATE finance.receivables SET status='OUTSTANDING' WHERE status='WRITTEN_OFF'")
    op.create_check_constraint(
        "ck_ar_status", "receivables",
        "status IN ('OUTSTANDING','PARTIAL','COLLECTED')", schema="finance")
