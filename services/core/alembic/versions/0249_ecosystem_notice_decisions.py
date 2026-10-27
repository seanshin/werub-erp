"""0249 — 결정 대기함 시드(공개판: 비움).

개발 기관이 다른 시스템과 맞물린 결정 항목을 이 리비전에서 결정 대기함에 넣었다. 그 항목은 그 기관의
운영 사정이라 공개판에서는 아무것도 넣지 않는다 — 리비전 사슬을 지키려고 파일만 남긴다.

Revision ID: 0249
Revises: 0248
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0249"
down_revision = "0248"
branch_labels = None
depends_on = None

SEEDS: list = []  # 공개판: 비움


def upgrade() -> None:
    conn = op.get_bind()
    for key, cat, title, question, options, ref in SEEDS:
        conn.execute(sa.text(
            "INSERT INTO common.pending_decisions (key, category, title, question, options, "
            "context_ref) VALUES (:k, :c, :t, :q, :o, :r) ON CONFLICT (key) DO NOTHING"),
            {"k": key, "c": cat, "t": title, "q": question,
             "o": json.dumps(options, ensure_ascii=False), "r": ref})


def downgrade() -> None:
    conn = op.get_bind()
    # 아직 PENDING인 것만 지운다 — 사람이 결정한 기록은 되돌림으로 지우지 않는다
    conn.execute(sa.text("DELETE FROM common.pending_decisions WHERE key = ANY(:ks) "
                         "AND status = 'PENDING'"), {"ks": [s[0] for s in SEEDS]})
