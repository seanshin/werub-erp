"""의료계약 생애주기 일원화 — status 확장·발효/승인/해지 컬럼 (계약 통합 PR-2).

위수탁계약과 동일한 8단계 상태머신 적용. 기존 ENDED→TERMINATED 무손실 백필, status VARCHAR(8)→(16)
(PENDING_APPROVAL 등 수용). 컬럼 effective_on(valid_from 백필)·approved_by/at·terminate_reason·terminated_on.
CHECK는 모델명·PG자동명 둘 다 IF EXISTS(0061 교훈).
"""
from alembic import op

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None

_NEW = ("'DRAFT','PENDING_APPROVAL','APPROVED','ACTIVE','EXPIRING','EXPIRED',"
        "'TERMINATED','REJECTED'")


def upgrade() -> None:
    op.execute("ALTER TABLE hr.medical_contracts ADD COLUMN IF NOT EXISTS effective_on DATE")
    op.execute("ALTER TABLE hr.medical_contracts ADD COLUMN IF NOT EXISTS approved_by BIGINT")
    op.execute("ALTER TABLE hr.medical_contracts ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ")
    op.execute("ALTER TABLE hr.medical_contracts ADD COLUMN IF NOT EXISTS "
               "terminate_reason VARCHAR(200)")
    op.execute("ALTER TABLE hr.medical_contracts ADD COLUMN IF NOT EXISTS terminated_on DATE")
    op.execute("UPDATE hr.medical_contracts SET effective_on = valid_from WHERE effective_on IS NULL")
    # status 폭 확장 + CHECK 재정의 + ENDED→TERMINATED 백필
    op.execute("ALTER TABLE hr.medical_contracts DROP CONSTRAINT IF EXISTS ck_mc_status")
    op.execute("ALTER TABLE hr.medical_contracts "
               "DROP CONSTRAINT IF EXISTS medical_contracts_status_check")
    op.execute("ALTER TABLE hr.medical_contracts ALTER COLUMN status TYPE VARCHAR(16)")
    op.execute("UPDATE hr.medical_contracts SET status='TERMINATED', "
               "terminated_on=COALESCE(terminated_on, valid_to) WHERE status='ENDED'")
    op.execute(f"ALTER TABLE hr.medical_contracts ADD CONSTRAINT ck_mc_status "
               f"CHECK (status IN ({_NEW}))")


def downgrade() -> None:
    op.execute("ALTER TABLE hr.medical_contracts DROP CONSTRAINT IF EXISTS ck_mc_status")
    # 확장 상태를 3값으로 환원(되돌릴 수 없는 신규 상태는 ENDED로 수렴)
    op.execute("UPDATE hr.medical_contracts SET status='ENDED' "
               "WHERE status NOT IN ('ACTIVE','DRAFT')")
    op.execute("ALTER TABLE hr.medical_contracts ALTER COLUMN status TYPE VARCHAR(8)")
    op.execute("ALTER TABLE hr.medical_contracts ADD CONSTRAINT ck_mc_status "
               "CHECK (status IN ('ACTIVE','ENDED','DRAFT'))")
    for col in ("terminated_on", "terminate_reason", "approved_at", "approved_by", "effective_on"):
        op.execute(f"ALTER TABLE hr.medical_contracts DROP COLUMN IF EXISTS {col}")
