"""0181 — SW·클라우드 서비스 등록 확대.

도메인·홈페이지 링크·API 토큰(암호화)·IT정보·영업 담당자·예상비용 단위(월/연)·VAT 포함·
결제 단위(토큰 등) 추가(요청). 서비스 담당자=기존 owner_employee_id·공급처=vendor·비고=note.

Revision ID: 0181
"""
import sqlalchemy as sa
from alembic import op

revision = "0181"
down_revision = "0180"
branch_labels = None
depends_on = None

_COLS = [
    ("domain", sa.String(120)),
    ("homepage_url", sa.String(200)),
    ("api_token_enc", sa.LargeBinary()),
    ("tech_info", sa.Text()),
    ("sales_contact", sa.String(120)),
    ("cost_period", sa.String(8)),
    ("vat_included", sa.Boolean()),
    ("usage_unit", sa.String(16)),
]


def upgrade() -> None:
    for name, type_ in _COLS:
        op.add_column("software_services", sa.Column(name, type_), schema="itam")


def downgrade() -> None:
    for name, _ in reversed(_COLS):
        op.drop_column("software_services", name, schema="itam")
