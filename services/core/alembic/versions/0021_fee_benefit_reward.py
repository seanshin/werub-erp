"""0021 — regulatory.fee_master 급여구분·보상률 (치료재료 enrichment).

치료재료(MATERIAL) criteria "중분류 | 제조사 | 재질 | 급여구분 | 보상률"에서 구조화 추출.

Revision ID: 0021
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE regulatory.fee_master ADD COLUMN benefit_type VARCHAR(8)")
    op.execute("ALTER TABLE regulatory.fee_master ADD COLUMN reward_rate NUMERIC(4,2)")
    # 치료재료 백필 — criteria 4번째=급여구분, 5번째=보상률 (" | " 구분)
    op.execute("""
        UPDATE regulatory.fee_master SET
            benefit_type = NULLIF(trim(split_part(criteria_text, '|', 4)), ''),
            reward_rate = CASE
                WHEN trim(split_part(criteria_text, '|', 5)) ~ '^[0-9]+(\\.[0-9]+)?$'
                THEN trim(split_part(criteria_text, '|', 5))::numeric ELSE NULL END
        WHERE code_type = 'MATERIAL' AND criteria_text IS NOT NULL
    """)
    op.execute("CREATE INDEX ix_fee_benefit ON regulatory.fee_master (code_type, benefit_type)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS regulatory.ix_fee_benefit")
    op.execute("ALTER TABLE regulatory.fee_master DROP COLUMN benefit_type")
    op.execute("ALTER TABLE regulatory.fee_master DROP COLUMN reward_rate")
