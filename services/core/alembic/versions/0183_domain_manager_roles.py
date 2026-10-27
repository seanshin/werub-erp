"""0183 — 도메인 관리자 역할 추가(IT/회계/물류 관리자).

common.users.role CHECK 제약에 IT_MANAGER·FINANCE_MANAGER·LOGISTICS_MANAGER 추가(요청·#5).
초기 마이그 0001이 무명 인라인 CHECK로 생성 → 실제 제약명은 users_role_check(Postgres 자동).
이 마이그에서 users_role_check를 드롭하고 명명 제약 ck_users_role로 재생성(모델과 정합).
(WORKSTATION은 users가 아닌 WorkstationPass라 제약 대상 아님 — 기존과 동일)

Revision ID: 0183
"""
from alembic import op

revision = "0183"
down_revision = "0182"
branch_labels = None
depends_on = None

_OLD = ("DIRECTOR", "ADMIN", "MANAGER", "STAFF")
_NEW = _OLD + ("IT_MANAGER", "FINANCE_MANAGER", "LOGISTICS_MANAGER")


def _drop_existing() -> None:
    # 0001의 무명 인라인 CHECK는 users_role_check로 자동 명명됨. 존재하는 쪽을 드롭.
    op.execute("ALTER TABLE common.users DROP CONSTRAINT IF EXISTS users_role_check")
    op.execute("ALTER TABLE common.users DROP CONSTRAINT IF EXISTS ck_users_role")


def _create(roles: tuple[str, ...]) -> None:
    vals = ", ".join(f"'{r}'" for r in roles)
    op.create_check_constraint("ck_users_role", "users", f"role IN ({vals})", schema="common")


def upgrade() -> None:
    _drop_existing()
    _create(_NEW)


def downgrade() -> None:
    _drop_existing()
    _create(_OLD)
