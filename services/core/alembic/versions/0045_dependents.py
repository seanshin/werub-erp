"""0045 — 부양가족 마스터: hr.dependents (인사 P3 마지막 잔여).

연말정산 인적공제·급여 간이세액의 부양가족 수를 매월 수동 입력 대신 마스터로:
관계·생년월일 기반 공제대상 자동 제안(자녀 20세 이하·직계존속 60세 이상·배우자·장애인)
+ 담당자 확정(counted) — 소득요건 등 비보유 정보는 담당자 판단.

Revision ID: 0045
"""
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE hr.dependents (
            id BIGSERIAL PRIMARY KEY,
            employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
            name VARCHAR(80) NOT NULL,
            relation VARCHAR(12) NOT NULL CHECK (relation IN
                ('SPOUSE','CHILD','PARENT','GRANDPARENT','SIBLING','OTHER')),
            birth_date DATE,
            disabled BOOLEAN NOT NULL DEFAULT false,
            counted BOOLEAN NOT NULL DEFAULT true,
            note TEXT,
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_dependents_emp ON hr.dependents (employee_id)")


def downgrade() -> None:
    op.execute("DROP TABLE hr.dependents")
