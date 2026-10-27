"""0013 — claims.pre_review_rules (사전심사 룰엔진 FR-41) + 샘플 룰 시드.

샘플 4종은 고시 협의 없이 일반 사례(중복·산정횟수·미등재코드·금액이상)로 선행. 고시 기반
세부 룰은 의료보험팀 협의(D41) 후 추가.

Revision ID: 0013
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE claims.pre_review_rules (
            id BIGSERIAL PRIMARY KEY,
            code VARCHAR(30) UNIQUE NOT NULL,
            name VARCHAR(120) NOT NULL,
            rule_type VARCHAR(20) NOT NULL
                CHECK (rule_type IN ('DUPLICATE','COUNT_LIMIT','UNKNOWN_CODE','AMOUNT_OUTLIER')),
            params JSONB,
            severity VARCHAR(8) NOT NULL DEFAULT 'WARN'
                CHECK (severity IN ('WARN','BLOCK')),
            active BOOLEAN NOT NULL DEFAULT true
        )
    """)
    op.execute("""
        INSERT INTO claims.pre_review_rules (code, name, rule_type, params, severity) VALUES
            ('PR-DUP','동일 진료일 동일코드 중복청구','DUPLICATE', NULL, 'WARN'),
            ('PR-CNT','동일 진료일 산정횟수 초과','COUNT_LIMIT','{"max": 1}', 'WARN'),
            ('PR-UNK','수가 마스터 미등재 코드','UNKNOWN_CODE', NULL, 'BLOCK'),
            ('PR-AMT','수가 표준금액 대비 이상치','AMOUNT_OUTLIER','{"tolerance": 0.1}', 'WARN')
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS claims.pre_review_rules")
