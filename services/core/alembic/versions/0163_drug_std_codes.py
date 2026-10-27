"""0163 — 의약품 표준코드 마스터(regulatory.drug_std_codes).

심평원 약가마스터_의약품표준코드 공개파일 적재용 — 표준코드(GS1)↔품목기준↔제품코드(보험)↔성분코드
브리지. 데이터 적재는 scripts/import_drug_std_codes.py(전량 교체), 마이그는 테이블만 생성.

Revision ID: 0163
"""
import sqlalchemy as sa
from alembic import op

revision = "0163"
down_revision = "0162"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "drug_std_codes",
        sa.Column("std_code", sa.String(14), primary_key=True),
        sa.Column("item_seq", sa.String(12), nullable=True),
        sa.Column("product_code", sa.String(12), nullable=True),
        sa.Column("ingredient_code", sa.String(12), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("company", sa.String(120), nullable=True),
        sa.Column("spec", sa.String(120), nullable=True),
        sa.Column("pack_qty", sa.Integer(), nullable=True),
        sa.Column("form", sa.String(40), nullable=True),
        sa.Column("rx_otc", sa.String(20), nullable=True),
        sa.Column("rep_code", sa.String(14), nullable=True),
        sa.Column("atc", sa.String(80), nullable=True),
        sa.Column("special_class", sa.String(60), nullable=True),
        sa.Column("canceled_on", sa.Date(), nullable=True),
        sa.Column("source_file", sa.String(120), nullable=True),
        schema="regulatory",
    )
    op.create_index("ix_drug_std_item_seq", "drug_std_codes", ["item_seq"],
                    schema="regulatory")
    op.create_index("ix_drug_std_product", "drug_std_codes", ["product_code"],
                    schema="regulatory")


def downgrade() -> None:
    op.drop_table("drug_std_codes", schema="regulatory")
