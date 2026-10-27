"""0051 — 의료진 질의/회신 (P2 삭감 방지 협업 루프).

심사팀이 적발/라인에 대해 의료진에게 근거·수정 요청 → 회신(근거|수정) → 적발 해소/DISMISS.

Revision ID: 0051
"""
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE claims.pre_review_queries (
            id BIGSERIAL PRIMARY KEY,
            case_id BIGINT NOT NULL REFERENCES claims.pre_review_cases(id),
            finding_id BIGINT REFERENCES claims.pre_review_findings(id),
            line_code VARCHAR(20),
            status VARCHAR(10) NOT NULL DEFAULT 'OPEN'
                CHECK (status IN ('OPEN','ANSWERED','ACCEPTED','REVISED')),
            question TEXT NOT NULL,
            raised_by BIGINT NOT NULL,
            assigned_to BIGINT,
            resolution_note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX ix_prq_case ON claims.pre_review_queries(case_id)")
    op.execute("CREATE INDEX ix_prq_status ON claims.pre_review_queries(status)")
    op.execute("CREATE INDEX ix_prq_assigned ON claims.pre_review_queries(assigned_to)")
    op.execute("""
        CREATE TABLE claims.pre_review_replies (
            id BIGSERIAL PRIMARY KEY,
            query_id BIGINT NOT NULL REFERENCES claims.pre_review_queries(id),
            reply_type VARCHAR(10) NOT NULL CHECK (reply_type IN ('EVIDENCE','REVISE')),
            body TEXT NOT NULL,
            author BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_prr_query ON claims.pre_review_replies(query_id)")


def downgrade() -> None:
    op.execute("DROP TABLE claims.pre_review_replies")
    op.execute("DROP TABLE claims.pre_review_queries")
