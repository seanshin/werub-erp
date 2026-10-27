"""0052 — 진료비 항목 분류(fee_master.benefit_category) + 보훈 coverage.

급여 6분류·비급여 4분류를 fee_master에 1회 계산 저장(명칭 키워드). 명세 그룹핑에 사용.
billing_classify.classify_benefit과 동일 규칙(SQL CASE).

Revision ID: 0052
"""
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE regulatory.fee_master ADD COLUMN benefit_category VARCHAR(16)")
    # 명칭 키워드 백필 — classify_benefit과 동일 우선순위
    op.execute(r"""
        UPDATE regulatory.fee_master SET benefit_category = CASE
          WHEN code_type = 'DRUG' THEN 'DRUG'
          WHEN code_type = 'MATERIAL' THEN 'MATERIAL'
          WHEN code_type = 'NONCOVERED' AND (name LIKE '%병실%' OR name LIKE '%상급병상%'
               OR name LIKE '%1인실%' OR name LIKE '%특실%' OR name LIKE '%병상%') THEN 'ROOM'
          WHEN code_type = 'NONCOVERED' AND (name LIKE '%선택진료%' OR name LIKE '%특진%') THEN 'SELECT'
          WHEN code_type = 'NONCOVERED' AND (name LIKE '%진단서%' OR name LIKE '%증명%'
               OR name LIKE '%사본%' OR name LIKE '%확인서%' OR name LIKE '%소견서%') THEN 'CERTIFICATE'
          WHEN code_type = 'NONCOVERED' AND (name LIKE '%검사%' OR name LIKE '%검진%'
               OR name LIKE '%초음파%' OR name LIKE '%MRI%' OR name LIKE '%CT%'
               OR name LIKE '%내시경%') THEN 'SPECIAL_EXAM'
          WHEN code_type = 'NONCOVERED' THEN 'ETC_NC'
          WHEN name LIKE '%식대%' OR name LIKE '%식사%' OR name LIKE '%경관영양%' THEN 'MEAL'
          WHEN name LIKE '%입원료%' OR name LIKE '%입원관리%' OR name LIKE '%입원환자%'
               OR name LIKE '%격리실%' OR name LIKE '%병실료%' THEN 'ADMISSION'
          WHEN name LIKE '%진찰료%' OR name LIKE '%외래관리료%' THEN 'CONSULT'
          ELSE 'ACT'
        END
    """)
    # 보훈(국가유공자) coverage — billing_lines CHECK는 없음(varchar) — 매핑만 추가됨


def downgrade() -> None:
    op.execute("ALTER TABLE regulatory.fee_master DROP COLUMN benefit_category")
