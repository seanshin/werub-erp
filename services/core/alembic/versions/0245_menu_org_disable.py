"""0245 — 기관 단위 메뉴 비활성(그룹웨어 중복 기능 숨김).

그룹웨어 전자결재가 별도로 있는 기관에서는 ERP의 셀프서비스 화면(휴가 신청·경비청구·근태 등)이
**중복 입구**가 된다. 기존 `MenuPermission`은 (menu_key, role) 단위라 `_EVERYONE` 항목을 완전히
숨기려면 역할 7개를 전부 꺼야 하고, 하나라도 놓치면 그 역할에는 계속 노출된다(운영 취약).

→ 기관 단위 단일 스위치 `org_settings.disabled_menu_keys`(JSON 배열)를 둔다. 역할 무관 전역 숨김.
   권한 체계는 건드리지 않는다 — **메뉴 노출만** 제어하고 EP·데이터는 그대로 둔다
   (HIS 연동 수신·기존 데이터 조회가 계속 필요하기 때문).

Revision ID: 0245
Revises: 0244
"""
import sqlalchemy as sa
from alembic import op

revision = "0245"
down_revision = "0244"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("org_settings",
                  sa.Column("disabled_menu_keys", sa.JSON(), nullable=True),
                  schema="common")


def downgrade() -> None:
    op.drop_column("org_settings", "disabled_menu_keys", schema="common")
