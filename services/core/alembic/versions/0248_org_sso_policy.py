"""0248 — HIS SSO 정책을 관리화면에서 결정(기관 설정). **2026-09-22 이전 HIS**의 token 방식은 HIS 메인 액세스 토큰(수 시간
유효·aud 'his'·jti 없음)을 URL로 보냈다 — 같은 토큰을 반복 수락하고, HIS의 ERP 허용 역할 게이트를
거치지 않으며, `HIS_JWT_AUDIENCE`를 설정하면 401로 끊긴다. 이후 HIS(R31·`1c6e7cd98`)는 token 방식도
ERP 전용 60초 토큰(aud 'erp-sso'·jti·역할 게이트)을 보낸다 — 이때는 aud 강제만으로 충분하다.

어느 방식을 남길지는 HIS 배치와 맞물린 **운영 결정**이라 코드 기본값으로 정하지 않고
`org_settings.sso_policy`(JSON)로 둔다. NULL(미결정)이면 종전 동작 그대로 + 관리화면 경고.

Revision ID: 0248
Revises: 0247
"""
import sqlalchemy as sa
from alembic import op

revision = "0248"
down_revision = "0247"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("org_settings", sa.Column("sso_policy", sa.JSON(), nullable=True),
                  schema="common")


def downgrade() -> None:
    op.drop_column("org_settings", "sso_policy", schema="common")
