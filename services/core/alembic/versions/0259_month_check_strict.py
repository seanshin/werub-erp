"""0259 — 월 CHECK 엄격화(자리마다 숫자) + 원가 테이블 2종 추가 (2026-10-10).

0258 식(길이 7·5번째 '-'·월 문자열 범위)은 "abcd-07"·"2026-0:"를 통과시켰다(독립 검토 LOW). 자리마다 ASCII 0~9를 IN 목록으로 확인하는
식(로캘 정렬 무관)으로 교체하고, 원가동인 값·원가 배부 스냅샷의 period에도 건다. prod 실측(읽기 전용): 원가동인 2·스냅샷 182 전부 정규형.
식은 이 파일에 고정한다(마이그레이션은 시점 스냅샷 — common.months.ym_check_sql과 같은 식).

Revision ID: 0259
Revises: 0258
"""
from alembic import op

revision = "0259"
down_revision = "0258"
branch_labels = None
depends_on = None

_HR = [
    ("duty_schedules", "yyyymm", "ck_duty_yyyymm"),
    ("payroll_records", "yyyymm", "ck_payroll_yyyymm"),
    ("attendance_summaries", "yyyymm", "ck_attendance_yyyymm"),
    ("incentive_calculations", "period", "ck_incentive_period"),
    ("retirement_provisions", "period", "ck_retire_prov_period"),
]
_FIN = [
    ("cost_driver_values", "period", "ck_driver_value_period"),
    ("cost_allocation_snapshots", "period", "ck_cost_snap_period"),
]


def _strict(col: str) -> str:
    digit = "('0','1','2','3','4','5','6','7','8','9')"
    digits = " AND ".join(f"substr({col}, {i}, 1) IN {digit}" for i in (1, 2, 3, 4, 6, 7))
    months = "(" + ",".join(f"'{m:02d}'" for m in range(1, 13)) + ")"
    return (f"length({col}) = 7 AND substr({col}, 5, 1) = '-' AND {digits} "
            f"AND substr({col}, 6, 2) IN {months}")


def _loose(col: str) -> str:   # 0258 식
    return (f"length({col}) = 7 AND substr({col}, 5, 1) = '-' "
            f"AND substr({col}, 6, 2) BETWEEN '01' AND '12'")


def upgrade() -> None:
    for table, col, name in _HR:
        op.drop_constraint(name, table, type_="check", schema="hr")
        op.create_check_constraint(name, table, _strict(col), schema="hr")
    for table, col, name in _FIN:
        op.create_check_constraint(name, table, _strict(col), schema="finance")


def downgrade() -> None:
    for table, _col, name in reversed(_FIN):
        op.drop_constraint(name, table, type_="check", schema="finance")
    for table, col, name in reversed(_HR):
        op.drop_constraint(name, table, type_="check", schema="hr")
        op.create_check_constraint(name, table, _loose(col), schema="hr")
