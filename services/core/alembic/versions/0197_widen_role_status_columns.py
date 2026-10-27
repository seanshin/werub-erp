"""0197 — 자체 유효값을 담지 못하는 varchar 컬럼 확대(P2-7 PG 레인 적발).

전체 테스트 PG 레인(sqlite는 길이 미강제·PG는 강제)이 "컬럼이 자기 enum 값을 못 담는" 잠복 결함 3종 적발:
- common.users.role / common.menu_permissions.role: String(16) < "LOGISTICS_MANAGER"(17자) → 역할 부여 시 에러
- finance.contingencies.status: String(10) < "PROVISIONED"(11자) → 충당부채 인식 시 에러
- scm.medical_waste_logs.status: String(10) < "HANDED_OVER"(11자·CHECK 허용값인데) → 인계완료 처리 시 에러

varchar 길이 확대는 PG에서 테이블 재작성 없는 온라인 변경(안전).

Revision ID: 0197
"""
import sqlalchemy as sa
from alembic import op

revision = "0197"
down_revision = "0196"
branch_labels = None
depends_on = None


_WIDEN = [
    ("common", "users", "role", 32, 16),
    ("common", "menu_permissions", "role", 32, 16),
    ("finance", "contingencies", "status", 16, 10),
    ("scm", "medical_waste_logs", "status", 16, 10),
]


def upgrade() -> None:
    for schema, table, col, new_len, _old in _WIDEN:
        op.alter_column(table, col, schema=schema,
                        existing_type=sa.String(_old), type_=sa.String(new_len),
                        existing_nullable=False)


def downgrade() -> None:
    for schema, table, col, new_len, old_len in _WIDEN:
        op.alter_column(table, col, schema=schema,
                        existing_type=sa.String(new_len), type_=sa.String(old_len),
                        existing_nullable=False)
