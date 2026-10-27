"""0008 — finance.fixed_assets·depreciation_entries (고정자산·감가상각) + 관련 계정과목.

Revision ID: 0008
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE finance.fixed_assets (
            id BIGSERIAL PRIMARY KEY,
            asset_no VARCHAR(30) UNIQUE NOT NULL,
            name VARCHAR(120) NOT NULL,
            category VARCHAR(12) NOT NULL DEFAULT 'MEDICAL'
                CHECK (category IN ('MEDICAL','FIXTURE','BUILDING','VEHICLE','OTHER')),
            acquired_on DATE NOT NULL,
            acquisition_cost NUMERIC(16,0) NOT NULL,
            salvage_value NUMERIC(16,0) NOT NULL DEFAULT 0,
            useful_life_years INTEGER NOT NULL,
            method VARCHAR(10) NOT NULL DEFAULT 'STRAIGHT'
                CHECK (method IN ('STRAIGHT','DECLINING')),
            department_id BIGINT,
            status VARCHAR(10) NOT NULL DEFAULT 'IN_USE'
                CHECK (status IN ('IN_USE','DISPOSED')),
            accumulated_depreciation NUMERIC(16,0) NOT NULL DEFAULT 0,
            disposed_on DATE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE finance.depreciation_entries (
            id BIGSERIAL PRIMARY KEY,
            asset_id BIGINT NOT NULL REFERENCES finance.fixed_assets(id),
            period VARCHAR(7) NOT NULL,
            amount NUMERIC(16,0) NOT NULL,
            book_value_after NUMERIC(16,0) NOT NULL,
            voucher_ref VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_depreciation_period UNIQUE (asset_id, period)
        )
    """)
    op.execute("CREATE INDEX ix_depr_asset ON finance.depreciation_entries (asset_id)")
    # 감가상각·자산 계정과목 (멱등) — 감가상각누계액은 자산 차감(contra, 대변잔액)
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES
            ('1710','의료장비','ASSET'),
            ('1810','감가상각누계액','ASSET'),
            ('2110','미지급금','LIABILITY'),
            ('5210','감가상각비','EXPENSE')
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS finance.depreciation_entries")
    op.execute("DROP TABLE IF EXISTS finance.fixed_assets")
    op.execute("DELETE FROM finance.accounts WHERE code IN ('1710','1810','5210')")
