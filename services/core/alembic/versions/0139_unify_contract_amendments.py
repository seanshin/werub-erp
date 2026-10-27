"""계약 변경이력 통합 — scm.contract_amendments → common.contract_amendments (계약 통합 PR-1).

위수탁·의료 계약이 공유하는 공통 이력 테이블로 승격. contract_kind(MEDICAL|OUTSOURCING)+contract_id로
다형 식별(스키마 교차 FK 회피). 기존 scm 행은 kind='OUTSOURCING'으로 무손실 이관 후 scm 테이블 제거.
"""
from alembic import op

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS common.contract_amendments (
            id BIGSERIAL PRIMARY KEY,
            contract_kind VARCHAR(12) NOT NULL,
            contract_id BIGINT NOT NULL,
            change_type VARCHAR(10) NOT NULL,
            old_value VARCHAR(60),
            new_value VARCHAR(60),
            effective_on DATE,
            reason VARCHAR(300),
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_camend_kind CHECK (contract_kind IN ('MEDICAL','OUTSOURCING')),
            CONSTRAINT ck_camend_type CHECK (change_type IN ('AMOUNT','PERIOD','RENEW'))
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_camend_kind_contract "
               "ON common.contract_amendments (contract_kind, contract_id)")
    # 기존 위수탁 이력 무손실 이관(테이블 존재 시)
    op.execute("""
        INSERT INTO common.contract_amendments
            (contract_kind, contract_id, change_type, old_value, new_value,
             effective_on, reason, created_by, created_at)
        SELECT 'OUTSOURCING', contract_id, change_type, old_value, new_value,
               effective_on, reason, created_by, created_at
        FROM scm.contract_amendments
        WHERE EXISTS (SELECT 1 FROM information_schema.tables
                      WHERE table_schema='scm' AND table_name='contract_amendments')
    """)
    op.execute("DROP INDEX IF EXISTS scm.ix_camend_contract")
    op.execute("DROP TABLE IF EXISTS scm.contract_amendments")


def downgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS scm.contract_amendments (
            id BIGSERIAL PRIMARY KEY,
            contract_id BIGINT NOT NULL REFERENCES scm.outsourcing_contracts(id),
            change_type VARCHAR(10) NOT NULL,
            old_value VARCHAR(60),
            new_value VARCHAR(60),
            effective_on DATE,
            reason VARCHAR(300),
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_camend_type CHECK (change_type IN ('AMOUNT','PERIOD','RENEW'))
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_camend_contract "
               "ON scm.contract_amendments (contract_id)")
    op.execute("""
        INSERT INTO scm.contract_amendments
            (contract_id, change_type, old_value, new_value,
             effective_on, reason, created_by, created_at)
        SELECT contract_id, change_type, old_value, new_value,
               effective_on, reason, created_by, created_at
        FROM common.contract_amendments WHERE contract_kind='OUTSOURCING'
    """)
    op.execute("DROP TABLE IF EXISTS common.contract_amendments")
