"""0039 — Tier2 통계 이상치 룰(STAT_OUTLIER): 배치 내 코드별 단가 분포 기반.

고정 허용치(AMOUNT_OUTLIER)와 달리 같은 실행 배치의 코드별 단가 분포(percentile)
대비 이상 라인을 적발 — 대량 붙여넣기 평가에서 의미. (claims-worklist PR-B)

Revision ID: 0039
"""
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_TYPES = ("'DUPLICATE','COUNT_LIMIT','UNKNOWN_CODE','AMOUNT_OUTLIER',"
          "'AGE_SEX_LIMIT','EXCLUSIVE_PAIR','PERIOD_LIMIT','SURCHARGE_CHECK',"
          "'LOS_RATIO','STAT_OUTLIER'")


def upgrade() -> None:
    op.execute("ALTER TABLE claims.pre_review_rules DROP CONSTRAINT IF EXISTS ck_prerule_type")
    op.execute("ALTER TABLE claims.pre_review_rules ADD CONSTRAINT ck_prerule_type "
               f"CHECK (rule_type IN ({_TYPES}))")
    op.execute("""
        INSERT INTO claims.pre_review_rules (code, name, rule_type, params, severity, active)
        VALUES ('PR-T2-STAT', '배치 내 단가 분포 이상(통계)', 'STAT_OUTLIER',
                '{"percentile": 95, "factor": 1.5, "min_samples": 10}', 'WARN', true)
    """)


def downgrade() -> None:
    op.execute("DELETE FROM claims.pre_review_rules WHERE code = 'PR-T2-STAT'")
