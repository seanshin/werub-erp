"""0198 — 기관 규제 프로필 + 분기 재원환자수 대장(간호등급 설정·관리).

산정 엔진(간호등급)이 필요로 하는 기관 고정 파라미터(종별·허가병상)와 주기 실측치(분기 재원환자수)를
관리자가 설정·저장하는 자리. 산식은 이미 완성 — 입력 정보 관리처 부재(G2 표면) 해소.

Revision ID: 0198
"""
import sqlalchemy as sa
from alembic import op

revision = "0198"
down_revision = "0197"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "institution_profile",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("hospital_type", sa.String(20), server_default="HOSPITAL", nullable=False),
        sa.Column("licensed_beds", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("id = 1", name="ck_institution_profile_singleton"),
        sa.CheckConstraint(
            "hospital_type IN ('TERTIARY','GENERAL','HOSPITAL','CLINIC_BED_BASIS')",
            name="ck_institution_hospital_type"),
        schema="regulatory",
    )
    op.create_table(
        "nursing_census",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("patient_days", sa.Numeric(12, 2), nullable=False),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("entered_by", sa.BigInteger(), nullable=True),
        sa.Column("entered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("period_start", name="uq_nursing_census_period"),
        schema="regulatory",
    )


def downgrade() -> None:
    op.drop_table("nursing_census", schema="regulatory")
    op.drop_table("institution_profile", schema="regulatory")
