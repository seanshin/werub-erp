"""위수탁계약 생애주기 세분화 — status 확장·발효/승인/해지사유 컬럼·변경이력 (CLM 심화).

status: DRAFT·PENDING_APPROVAL·APPROVED·ACTIVE·EXPIRING·EXPIRED·TERMINATED·REJECTED(기존 3값 무손실).
컬럼: approved_by/at·effective_on·terminate_reason·terminated_on. contract_amendments(금액·기간·갱신 이력).
CHECK는 모델명·PG자동명 둘 다 IF EXISTS(0061 교훈). effective_on은 start_on 백필.
"""
from alembic import op
import sqlalchemy as sa

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None

_NEW = "'DRAFT','PENDING_APPROVAL','APPROVED','ACTIVE','EXPIRING','EXPIRED','TERMINATED','REJECTED'"


def upgrade() -> None:
    op.execute("ALTER TABLE scm.outsourcing_contracts ADD COLUMN IF NOT EXISTS approved_by BIGINT")
    op.execute("ALTER TABLE scm.outsourcing_contracts ADD COLUMN IF NOT EXISTS "
               "approved_at TIMESTAMPTZ")
    op.execute("ALTER TABLE scm.outsourcing_contracts ADD COLUMN IF NOT EXISTS effective_on DATE")
    op.execute("ALTER TABLE scm.outsourcing_contracts ADD COLUMN IF NOT EXISTS "
               "terminate_reason VARCHAR(200)")
    op.execute("ALTER TABLE scm.outsourcing_contracts ADD COLUMN IF NOT EXISTS terminated_on DATE")
    op.execute("UPDATE scm.outsourcing_contracts SET effective_on = start_on "
               "WHERE effective_on IS NULL")
    # status CHECK 확장 — 모델명·PG자동명 둘 다 제거 후 재생성
    op.execute("ALTER TABLE scm.outsourcing_contracts DROP CONSTRAINT IF EXISTS ck_oc_status")
    op.execute("ALTER TABLE scm.outsourcing_contracts "
               "DROP CONSTRAINT IF EXISTS outsourcing_contracts_status_check")
    op.execute(f"ALTER TABLE scm.outsourcing_contracts ADD CONSTRAINT ck_oc_status "
               f"CHECK (status IN ({_NEW}))")
    op.create_table(
        "contract_amendments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("contract_id", sa.BigInteger(),
                  sa.ForeignKey("scm.outsourcing_contracts.id"), nullable=False),
        sa.Column("change_type", sa.String(10), nullable=False),   # AMOUNT|PERIOD|RENEW
        sa.Column("old_value", sa.String(60)),
        sa.Column("new_value", sa.String(60)),
        sa.Column("effective_on", sa.Date()),
        sa.Column("reason", sa.String(300)),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("change_type IN ('AMOUNT','PERIOD','RENEW')", name="ck_camend_type"),
        schema="scm",
    )
    op.create_index("ix_camend_contract", "contract_amendments", ["contract_id"], schema="scm")


def downgrade() -> None:
    op.drop_table("contract_amendments", schema="scm")
    op.execute("ALTER TABLE scm.outsourcing_contracts DROP CONSTRAINT IF EXISTS ck_oc_status")
    op.execute("ALTER TABLE scm.outsourcing_contracts ADD CONSTRAINT ck_oc_status "
               "CHECK (status IN ('ACTIVE','EXPIRED','TERMINATED'))")
    for col in ("terminated_on", "terminate_reason", "effective_on", "approved_at", "approved_by"):
        op.execute(f"ALTER TABLE scm.outsourcing_contracts DROP COLUMN IF EXISTS {col}")
