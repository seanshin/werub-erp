"""0024 — 원가 배부 스냅샷(마감 기준) + 진료과 코드 매핑(dept_code↔department).

Revision ID: 0024
"""
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 진료과 코드(claims.dept_code = GS/IM/OS/PED 등) ↔ common.departments 매핑
    op.execute("ALTER TABLE common.departments ADD COLUMN code VARCHAR(8)")
    for code, name in [("IM", "내과"), ("OS", "정형외과"), ("GS", "외과"),
                       ("PED", "소아청소년과"), ("FM", "가정의학과"), ("ENT", "이비인후과"),
                       ("OPH", "안과"), ("DERM", "피부과"), ("URO", "비뇨의학과"),
                       ("OBGY", "산부인과"), ("PS", "성형외과"), ("NS", "신경외과"),
                       ("RM", "재활의학과"), ("PSY", "정신건강의학과")]:
        op.execute(f"UPDATE common.departments SET code = '{code}' WHERE name = '{name}'")
    op.execute("""
        CREATE TABLE finance.cost_allocation_snapshots (
            id BIGSERIAL PRIMARY KEY,
            period VARCHAR(7) NOT NULL,
            department_id BIGINT NOT NULL,
            department VARCHAR(80),
            labor NUMERIC(16,0) NOT NULL DEFAULT 0,
            material NUMERIC(16,0) NOT NULL DEFAULT 0,
            overhead NUMERIC(16,0) NOT NULL DEFAULT 0,
            total_cost NUMERIC(16,0) NOT NULL DEFAULT 0,
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_cost_snap UNIQUE (period, department_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE finance.cost_allocation_snapshots")
    op.execute("ALTER TABLE common.departments DROP COLUMN code")
