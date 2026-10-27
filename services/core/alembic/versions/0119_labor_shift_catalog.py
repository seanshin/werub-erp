"""0119 — FR-32 노동규제 실효화: hr.shift_types + regulatory.labor_rules.

죽은 룰(전공의 4주평균 80h) 되살리기 기반. 시프트당 시간을 카탈로그로 분리하고
(당직·온콜 장시간 시프트 등록 가능), 임계값을 DB로 이전(인사팀 확정용·G3).
duty_assignments의 고정 CHECK(D/E/N/OFF) 제거 → 카탈로그 검증으로 이전.
기본 4종(D/E/N/OFF)·기본 임계값 4건 시드 → 현행 동작 무변(하위호환).

Revision ID: 0119
"""
import sqlalchemy as sa
from alembic import op

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shift_types",
        sa.Column("code", sa.String(length=4), primary_key=True),
        sa.Column("label", sa.String(length=40), nullable=False),
        sa.Column("work_hours", sa.Numeric(4, 1), server_default="0"),
        sa.Column("is_night", sa.Boolean(), server_default=sa.false()),
        sa.Column("counts_as_workday", sa.Boolean(), server_default=sa.true()),
        sa.Column("active", sa.Boolean(), server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="hr",
    )
    op.create_table(
        "labor_rules",
        sa.Column("rule_key", sa.String(length=40), primary_key=True),
        sa.Column("threshold", sa.Numeric(6, 1), nullable=False),
        sa.Column("notice_ref", sa.String(length=120)),
        sa.Column("active", sa.Boolean(), server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="regulatory",
    )

    # 고정 CHECK 제거 — 카탈로그(shift_types)가 SoT. 당직 등 신규 코드 추가 시 마이그 불필요.
    # 0002는 CREATE TABLE 인라인 CHECK라 PG 자동명(duty_assignments_shift_check). 모델명(ck_duty_shift)
    # 양쪽 IF EXISTS로 환경 무관 멱등 제거.
    op.execute("ALTER TABLE hr.duty_assignments DROP CONSTRAINT IF EXISTS duty_assignments_shift_check")
    op.execute("ALTER TABLE hr.duty_assignments DROP CONSTRAINT IF EXISTS ck_duty_shift")

    # 기본 시드 — 현행 8h-시프트 동작 그대로(무변). 당직(OC) 등은 인사팀이 추후 등록.
    op.bulk_insert(
        sa.table(
            "shift_types",
            sa.column("code", sa.String), sa.column("label", sa.String),
            sa.column("work_hours", sa.Numeric), sa.column("is_night", sa.Boolean),
            sa.column("counts_as_workday", sa.Boolean), schema="hr",
        ),
        [
            {"code": "D", "label": "주간", "work_hours": 8, "is_night": False, "counts_as_workday": True},
            {"code": "E", "label": "저녁", "work_hours": 8, "is_night": False, "counts_as_workday": True},
            {"code": "N", "label": "야간", "work_hours": 8, "is_night": True, "counts_as_workday": True},
            {"code": "OFF", "label": "휴무", "work_hours": 0, "is_night": False, "counts_as_workday": False},
        ],
    )
    op.bulk_insert(
        sa.table(
            "labor_rules",
            sa.column("rule_key", sa.String), sa.column("threshold", sa.Numeric),
            sa.column("notice_ref", sa.String), schema="regulatory",
        ),
        [
            {"rule_key": "weekly_hours_general", "threshold": 52, "notice_ref": "근로기준법 §50·§53 (40h+연장 12h)"},
            {"rule_key": "resident_avg_4week", "threshold": 80, "notice_ref": "전공의법 §7① (4주 평균 주 80h)"},
            {"rule_key": "max_consecutive_nights", "threshold": 3, "notice_ref": "연속 야간 제한(권고)"},
            {"rule_key": "max_consecutive_workdays", "threshold": 6, "notice_ref": "근로기준법 §55 주휴 취지"},
        ],
    )


def downgrade() -> None:
    op.create_check_constraint("ck_duty_shift", "duty_assignments",
                               "shift IN ('D','E','N','OFF')", schema="hr")
    op.drop_table("labor_rules", schema="regulatory")
    op.drop_table("shift_types", schema="hr")
