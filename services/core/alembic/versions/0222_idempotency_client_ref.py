"""0222 — F5 멱등키: 의료외수익·위탁재고 사용 client_ref (재전송 중복 방지).

audit F5: ancillary.record·consignment.consume가 재전송(네트워크 재시도) 시 중복 수익/전표·
미지급을 생성(행잠금은 순차 재시도 무방어). client_ref(클라이언트 멱등키) 컬럼+부분 유니크 인덱스
(NULL 다중 허용)로 동시/재전송 중복의 DB 백스톱. 서비스는 선조회로 기존 레코드 반환(무전기 재생).

Revision ID: 0222
"""
from alembic import op
import sqlalchemy as sa

revision = "0222"
down_revision = "0221"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ancillary_revenues", sa.Column("client_ref", sa.String(64), nullable=True),
                  schema="finance")
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_ancillary_client_ref
        ON finance.ancillary_revenues (client_ref) WHERE client_ref IS NOT NULL
    """)
    op.add_column("consignment_consumptions", sa.Column("client_ref", sa.String(64), nullable=True),
                  schema="scm")
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_consign_consume_client_ref
        ON scm.consignment_consumptions (client_ref) WHERE client_ref IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS scm.ux_consign_consume_client_ref")
    op.drop_column("consignment_consumptions", "client_ref", schema="scm")
    op.execute("DROP INDEX IF EXISTS finance.ux_ancillary_client_ref")
    op.drop_column("ancillary_revenues", "client_ref", schema="finance")
