"""0258 — 인사 월 컬럼 저장형 "YYYY-MM" CHECK (2026-10-10).

라우터가 "2026-07"·"202607"을 둘 다 받아 원문 그대로 저장했다 — 유니크가 (직원, 원문)이라 같은 달 급여가 두 행이
될 수 있었고, [5:7] 슬라이스가 압축형에서 틀린 월을 읽었다. 진입점은 common.months로 정규화하고, 이 CHECK가
그 밖의 경로(스크립트·향후 코드)를 막는다. prod 실측(읽기 전용): 5개 테이블 전부 정규형(급여 445·근태 36·
근무표 1·성과급 46·퇴직충당 249) — 기존 행 검증이 통과한다.

Revision ID: 0258
Revises: 0257
"""
from alembic import op

revision = "0258"
down_revision = "0257"
branch_labels = None
depends_on = None

_TARGETS = [
    ("duty_schedules", "yyyymm", "ck_duty_yyyymm"),
    ("payroll_records", "yyyymm", "ck_payroll_yyyymm"),
    ("attendance_summaries", "yyyymm", "ck_attendance_yyyymm"),
    ("incentive_calculations", "period", "ck_incentive_period"),
    ("retirement_provisions", "period", "ck_retire_prov_period"),
]


def _expr(col: str) -> str:
    return (f"length({col}) = 7 AND substr({col}, 5, 1) = '-' "
            f"AND substr({col}, 6, 2) BETWEEN '01' AND '12'")


def upgrade() -> None:
    for table, col, name in _TARGETS:
        op.create_check_constraint(name, table, _expr(col), schema="hr")


def downgrade() -> None:
    for table, _col, name in reversed(_TARGETS):
        op.drop_constraint(name, table, type_="check", schema="hr")
