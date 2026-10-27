"""0046 — 비급여 코드 타입(NONCOVERED) + 급여 오청구 적발 룰.

고시 전문(제1편 제3부 행위 비급여 목록)에서 추출한 비급여 코드를 fee_master에
NONCOVERED 타입으로 적재(가격 없음 — 기관 자율가는 HIS NonCoveredItem 별도).
사전심사 NONCOVERED_CLAIM 룰: 비급여 코드의 급여 청구 라인 차단(BLOCK).

Revision ID: 0046
"""
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

_PRERULE_TYPES = ("'DUPLICATE','COUNT_LIMIT','UNKNOWN_CODE','AMOUNT_OUTLIER',"
                  "'AGE_SEX_LIMIT','EXCLUSIVE_PAIR','PERIOD_LIMIT','SURCHARGE_CHECK',"
                  "'LOS_RATIO','STAT_OUTLIER','NONCOVERED_CLAIM'")


def upgrade() -> None:
    # 0001은 인라인 CHECK(자동명 fee_master_code_type_check) — 양쪽 이름 모두 제거
    op.execute("ALTER TABLE regulatory.fee_master DROP CONSTRAINT IF EXISTS ck_fee_type")
    op.execute("ALTER TABLE regulatory.fee_master "
               "DROP CONSTRAINT IF EXISTS fee_master_code_type_check")
    op.execute("ALTER TABLE regulatory.fee_master ADD CONSTRAINT ck_fee_type "
               "CHECK (code_type IN ('TARIFF','DRUG','MATERIAL','NONCOVERED'))")
    op.execute("ALTER TABLE claims.pre_review_rules DROP CONSTRAINT IF EXISTS ck_prerule_type")
    op.execute("ALTER TABLE claims.pre_review_rules ADD CONSTRAINT ck_prerule_type "
               f"CHECK (rule_type IN ({_PRERULE_TYPES}))")
    op.execute("""
        INSERT INTO claims.pre_review_rules (code, name, rule_type, params, severity, active)
        VALUES ('PR-NC-CLAIM', '비급여 항목 급여 청구', 'NONCOVERED_CLAIM',
                '{}', 'BLOCK', true)
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM claims.pre_review_rules WHERE code = 'PR-NC-CLAIM'")
    op.execute("DELETE FROM regulatory.fee_master WHERE code_type = 'NONCOVERED'")
    op.execute("ALTER TABLE regulatory.fee_master DROP CONSTRAINT IF EXISTS ck_fee_type")
    op.execute("ALTER TABLE regulatory.fee_master ADD CONSTRAINT ck_fee_type "
               "CHECK (code_type IN ('TARIFF','DRUG','MATERIAL'))")
