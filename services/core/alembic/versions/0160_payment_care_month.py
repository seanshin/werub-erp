"""patient.payments 진료월(care_month) — 기수납·미수 귀속 스코프 정합.

_prepaid(billing)가 chart 전기간을 상계하는데 invoice는 encounter/진료월로 좁혀져 교차 인카운터
과다상계(환자 과다청구), _collected(발생주의)는 received_on 귀속이 target(treated_on)과 어긋나던 갭.
Payment에 진료월을 태깅(수납 시 reception 월)해 기수납·미수를 진료월 단위로 정합. 백필=reception 월.
"""
import sqlalchemy as sa
from alembic import op

revision = "0160"
down_revision = "0159"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("care_month", sa.String(length=7), nullable=True),
                  schema="patient")
    op.create_index("ix_payments_care_month", "payments", ["care_month"], schema="patient")
    # 백필 — 기존 수납은 접수(received_on) 월로 귀속
    op.execute("""
        UPDATE patient.payments p
        SET care_month = to_char(r.received_on, 'YYYY-MM')
        FROM patient.receptions r
        WHERE r.id = p.reception_id AND p.care_month IS NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_payments_care_month", table_name="payments", schema="patient")
    op.drop_column("payments", "care_month", schema="patient")
