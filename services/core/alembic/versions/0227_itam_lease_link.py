"""0227 — IT자산 리스 반영: itam.it_assets ↔ finance.lease_contracts 연결.

사용자 요청(2026-07-23): IT자산이 리스로 취득되는 경우 IFRS 16 리스계약(사용권자산·리스부채)을
반영. 구매(finance_asset_id) 외 리스 취득 경로 추가 — 상호 배타(둘 중 하나).

- itam.it_assets: acquisition_type(OWNED|LEASED) + lease_id(finance.lease_contracts 값링크·무FK).
- finance.lease_contracts: asset_kind(GENERAL|IT) + itam_asset_id(백링크) — IT 리스 식별.

Revision ID: 0227
"""
import sqlalchemy as sa
from alembic import op

revision = "0227"
down_revision = "0226"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("it_assets",
                  sa.Column("acquisition_type", sa.String(10), nullable=False,
                            server_default="OWNED"), schema="itam")
    op.add_column("it_assets", sa.Column("lease_id", sa.BigInteger(), nullable=True), schema="itam")
    op.create_check_constraint("ck_itasset_acqtype", "it_assets",
                               "acquisition_type IN ('OWNED','LEASED')", schema="itam")
    op.add_column("lease_contracts",
                  sa.Column("asset_kind", sa.String(10), nullable=False, server_default="GENERAL"),
                  schema="finance")
    op.add_column("lease_contracts", sa.Column("itam_asset_id", sa.BigInteger(), nullable=True),
                  schema="finance")
    op.create_check_constraint("ck_lease_asset_kind", "lease_contracts",
                               "asset_kind IN ('GENERAL','IT')", schema="finance")


def downgrade() -> None:
    op.execute("ALTER TABLE finance.lease_contracts DROP CONSTRAINT IF EXISTS ck_lease_asset_kind")
    op.drop_column("lease_contracts", "itam_asset_id", schema="finance")
    op.drop_column("lease_contracts", "asset_kind", schema="finance")
    op.execute("ALTER TABLE itam.it_assets DROP CONSTRAINT IF EXISTS ck_itasset_acqtype")
    op.drop_column("it_assets", "lease_id", schema="itam")
    op.drop_column("it_assets", "acquisition_type", schema="itam")
