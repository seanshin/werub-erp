"""0189 — 환불에 진료월(care_month) 귀속 컬럼 추가(기수납 순액 정합).

BillingService._prepaid가 PAID 수납만 합산하고 환불(Refund)을 차감하지 않아, 환불 발행 후에도
balance_due가 계속 음수(환불 대상)로 남아 유령 환불이 반복 가능하던 결함(전체검토 HIGH·현금누수).
환불을 진료월 스코프로 순차감하려면 Payment.care_month와 대칭인 care_month가 필요.
접수(received_on) 월 기준(PaymentService.pay와 동일 도출)·미연결 환불은 반환일(refunded_on) 월 폴백.

Revision ID: 0189
"""
import sqlalchemy as sa
from alembic import op

revision = "0189"
down_revision = "0188"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refunds", sa.Column("care_month", sa.String(7), nullable=True),
                  schema="patient")
    op.create_index("ix_patient_refunds_care_month", "refunds", ["care_month"],
                    schema="patient")
    # 백필 — 접수 연결 시 접수월(received_on), 없으면 반환일(refunded_on) 월(수납 도출과 대칭)
    op.execute("""
        UPDATE patient.refunds r
        SET care_month = to_char(rc.received_on, 'YYYY-MM')
        FROM patient.receptions rc
        WHERE r.reception_id = rc.id AND r.care_month IS NULL
    """)
    op.execute("""
        UPDATE patient.refunds
        SET care_month = to_char(refunded_on, 'YYYY-MM')
        WHERE care_month IS NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_patient_refunds_care_month", table_name="refunds", schema="patient")
    op.drop_column("refunds", "care_month", schema="patient")
