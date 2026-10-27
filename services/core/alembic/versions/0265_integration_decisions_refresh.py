"""연동 결정 5건 결정문 갱신(공개판: 비움).

개발 기관의 운영 실측으로 질문을 고친 리비전이다. 그 실측은 새 설치에 해당하지 않으므로 공개판은
질문을 바꾸지 않는다 — 리비전 사슬을 지키려고 파일만 남긴다.

Revision ID: 0265
Revises: 0264
"""
import sqlalchemy as sa
from alembic import op

revision = "0265"
down_revision = "0264"
branch_labels = None
depends_on = None

# (key, 원문, 갱신문)
_ROWS: list = []  # 공개판: 비움


def _swap(frm: int, to: int) -> None:
    bind = op.get_bind()
    for row in _ROWS:
        bind.execute(sa.text("UPDATE common.pending_decisions SET question = :new "
                             "WHERE key = :k AND status <> 'DECIDED' AND question = :old"),
                     {"k": row[0], "old": row[frm], "new": row[to]})


def upgrade() -> None:
    _swap(1, 2)


def downgrade() -> None:
    _swap(2, 1)
