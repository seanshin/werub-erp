"""0164 — 의약품 표준코드 마스터 상품명 trigram GIN 인덱스.

drug_std_codes(305K행) 상품명 폴백 조회(워크스테이션 이름검색·drug_matcher 표준코드 브리지)는
`name ILIKE '%q%'` 선행 와일드카드라 btree 불가 → 매 호출 30만행 seq scan. pg_trgm GIN으로 해소.
pg_trgm은 PG13+ trusted 확장(DB 소유 롤이 CREATE 가능). 확장 미보유 환경이면 배포 전 DBA가
`CREATE EXTENSION pg_trgm` 선행 필요.

Revision ID: 0164
"""
from alembic import op

revision = "0164"
down_revision = "0163"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_drug_std_name_trgm "
        "ON regulatory.drug_std_codes USING gin (name gin_trgm_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS regulatory.ix_drug_std_name_trgm")
