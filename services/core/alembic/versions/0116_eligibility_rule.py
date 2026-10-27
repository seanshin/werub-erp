"""0116 — 자격검증 심화(P1 G11): ELIGIBILITY_INVALID 룰타입 + 자동 룰.

자격상실(EXPIRED)·미확인(NOT_FOUND) 환자의 급여(비-NONCOVERED) 청구 적발 — 보험 거부 위험.
기존엔 급여룰에서 제외만 되고 미적발(조용히 누락) → 명시 적발로 전환.

Revision ID: 0116
"""
from alembic import op

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None

_TYPES = ("DUPLICATE','COUNT_LIMIT','UNKNOWN_CODE','AMOUNT_OUTLIER','AGE_SEX_LIMIT',"
          "'EXCLUSIVE_PAIR','PERIOD_LIMIT','SURCHARGE_CHECK','LOS_RATIO','STAT_OUTLIER',"
          "'NONCOVERED_CLAIM','ELIGIBILITY_INVALID")


def upgrade() -> None:
    op.drop_constraint("ck_prerule_type", "pre_review_rules", schema="claims")
    op.create_check_constraint("ck_prerule_type", "pre_review_rules",
                               f"rule_type IN ('{_TYPES}')", schema="claims")
    op.execute(
        "INSERT INTO claims.pre_review_rules (code, name, rule_type, params, severity, active) "
        "VALUES ('PR-ELIG-AUTO', '자격상실/미확인 급여청구', 'ELIGIBILITY_INVALID', NULL, 'WARN', true) "
        "ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM claims.pre_review_rules WHERE code = 'PR-ELIG-AUTO'")
    op.drop_constraint("ck_prerule_type", "pre_review_rules", schema="claims")
    op.create_check_constraint(
        "ck_prerule_type", "pre_review_rules",
        "rule_type IN ('DUPLICATE','COUNT_LIMIT','UNKNOWN_CODE','AMOUNT_OUTLIER','AGE_SEX_LIMIT',"
        "'EXCLUSIVE_PAIR','PERIOD_LIMIT','SURCHARGE_CHECK','LOS_RATIO','STAT_OUTLIER',"
        "'NONCOVERED_CLAIM')", schema="claims")
