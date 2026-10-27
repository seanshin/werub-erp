"""0014 — finance.cost_driver_values (ABC 원가동인 값 FR-42).

동인 값(CASES·AREA 등)은 원가팀 협의(D42) 후 입력. HEADCOUNT는 직원수 자동산출(미저장).
미입력 동인은 엔진이 HEADCOUNT로 폴백.

Revision ID: 0014
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE finance.cost_driver_values (
            id BIGSERIAL PRIMARY KEY,
            period VARCHAR(7) NOT NULL,
            department_id BIGINT NOT NULL,
            driver VARCHAR(16) NOT NULL,
            value NUMERIC(14,2) NOT NULL,
            CONSTRAINT uq_driver_value UNIQUE (period, department_id, driver)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS finance.cost_driver_values")
