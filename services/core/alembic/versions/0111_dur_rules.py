"""0111 — 약제 DUR(P0-1a): drug_ingredient_map + dur_rules.

DUR(의약품안전사용) 결정론 점검 기반:
- drug_ingredient_map: 보험 약가코드 → 주성분코드(성분 단위 DUR 매칭). HIS/약가마스터·식약처 적재.
- dur_rules: 병용금기(CONTRA 성분쌍)·동일성분 중복(DUP)·연령금기(AGE). 공개데이터(식약처/공공데이터포털) 적재.

엔진 evaluate_dur가 ingredient 부여된 라인에 적용. 실데이터 적재는 운영 단계(POST 적재 EP).

Revision ID: 0111
"""
import sqlalchemy as sa
from alembic import op

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "drug_ingredient_map",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("insurance_code", sa.String(length=20), nullable=False, unique=True),
        sa.Column("ingredient_code", sa.String(length=20), nullable=False),
        sa.Column("ingredient_name", sa.String(length=200)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="claims",
    )
    op.create_index("ix_drug_ingredient_code", "drug_ingredient_map",
                    ["ingredient_code"], schema="claims")
    op.create_table(
        "dur_rules",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("dur_type", sa.String(length=10), nullable=False),  # CONTRA|DUP|AGE|PREGNANCY
        sa.Column("ingredient_code", sa.String(length=20)),           # 대상 성분(또는 쌍의 A)
        sa.Column("ingredient_code_b", sa.String(length=20)),         # 병용금기 쌍의 B
        sa.Column("min_age", sa.Integer()),                           # 연령금기 하한(미만 금기)
        sa.Column("max_age", sa.Integer()),                           # 연령금기 상한(초과 금기)
        sa.Column("reason", sa.String(length=300)),                   # 금기사유
        sa.Column("notice_no", sa.String(length=40)),                # 공고번호(출처표시)
        sa.Column("severity", sa.String(length=8), server_default="BLOCK"),
        sa.Column("source", sa.String(length=20), server_default="MFDS"),  # 출처(식약처 등)
        sa.Column("active", sa.Boolean(), server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("dur_type IN ('CONTRA','DUP','AGE','PREGNANCY')", name="ck_dur_type"),
        schema="claims",
    )
    op.create_index("ix_dur_type_ingredient", "dur_rules",
                    ["dur_type", "ingredient_code"], schema="claims")


def downgrade() -> None:
    op.drop_table("dur_rules", schema="claims")
    op.drop_table("drug_ingredient_map", schema="claims")
