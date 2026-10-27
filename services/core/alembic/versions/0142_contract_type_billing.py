"""일반 구매계약 CLM 확장 — contract_type·billing_type (위수탁 외 구매/용역/임대 + 일회성).

기존 행은 OUTSOURCING/MONTHLY 무손실 백필. ONE_TIME은 월 accrue 제외(생애주기·이력은 동일).
"""
from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE scm.outsourcing_contracts ADD COLUMN IF NOT EXISTS "
               "contract_type VARCHAR(16) NOT NULL DEFAULT 'OUTSOURCING'")
    op.execute("ALTER TABLE scm.outsourcing_contracts ADD COLUMN IF NOT EXISTS "
               "billing_type VARCHAR(10) NOT NULL DEFAULT 'MONTHLY'")
    op.execute("ALTER TABLE scm.outsourcing_contracts DROP CONSTRAINT IF EXISTS ck_oc_type")
    op.execute("ALTER TABLE scm.outsourcing_contracts ADD CONSTRAINT ck_oc_type "
               "CHECK (contract_type IN ('OUTSOURCING','PURCHASE','SERVICE','LEASE','OTHER'))")
    op.execute("ALTER TABLE scm.outsourcing_contracts DROP CONSTRAINT IF EXISTS ck_oc_billing")
    op.execute("ALTER TABLE scm.outsourcing_contracts ADD CONSTRAINT ck_oc_billing "
               "CHECK (billing_type IN ('MONTHLY','ONE_TIME'))")


def downgrade() -> None:
    op.execute("ALTER TABLE scm.outsourcing_contracts DROP CONSTRAINT IF EXISTS ck_oc_billing")
    op.execute("ALTER TABLE scm.outsourcing_contracts DROP CONSTRAINT IF EXISTS ck_oc_type")
    for col in ("billing_type", "contract_type"):
        op.execute(f"ALTER TABLE scm.outsourcing_contracts DROP COLUMN IF EXISTS {col}")
