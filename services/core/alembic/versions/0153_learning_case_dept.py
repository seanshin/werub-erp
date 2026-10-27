"""claims.pre_review_learning_cases 부서 차원(dept_code) — 학습 사례 부서경계(IDOR/PII).

학습 사례가 chart_no·시술코드·금액(환자 식별성) 보유하나 dept 차원이 없어 목록/내보내기에 부서경계를
걸 수 없던 IDOR/PII 갭(claims 심층 재검토 2026-07-05). dept_code 추가 → 수확/생성 시 원 케이스 부서로
채우고, 부서장 스코프 목록·내보내기·수정을 본부서로 제한. 기존 행은 NULL(ADMIN만 열람).
"""
import sqlalchemy as sa
from alembic import op

revision = "0153"
down_revision = "0152"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pre_review_learning_cases",
                  sa.Column("dept_code", sa.String(length=10), nullable=True),
                  schema="claims")
    op.create_index("ix_prlc_dept_code", "pre_review_learning_cases", ["dept_code"],
                    schema="claims")


def downgrade() -> None:
    op.drop_index("ix_prlc_dept_code", table_name="pre_review_learning_cases", schema="claims")
    op.drop_column("pre_review_learning_cases", "dept_code", schema="claims")
