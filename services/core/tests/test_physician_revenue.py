"""의사별 진료실적 집계 (P4-2) — 수납(현금)·청구(발생)·삭감."""
from datetime import date

from erp.claims.models import ClaimBatch, ClaimStatement
from erp.hr.physician_revenue import PhysicianRevenueService
from erp.patient.models import Patient, Payment, Reception

svc = PhysicianRevenueService()


async def _payment(session, *, doctor_id, amount, paid_on, no):
    p = Patient(chart_no=no, name="환자", birth_date=date(1990, 1, 1))
    session.add(p)
    await session.flush()
    r = Reception(patient_id=p.id, dept_code="IM", doctor_id=doctor_id,
                  received_on=paid_on, source="HIS")
    session.add(r)
    await session.flush()
    session.add(Payment(reception_id=r.id, amount=amount, method="CARD",
                        paid_on=paid_on, status="PAID"))
    await session.flush()


async def _claim(session, *, doctor_id, claimed, adjusted, care_month, receipt):
    b = ClaimBatch(receipt_no=receipt, care_month=care_month,
                   notice_arrived_on=date(2026, 7, 1), claimed_amt=claimed)
    session.add(b)
    await session.flush()
    session.add(ClaimStatement(batch_id=b.id, stmt_serial="1", doctor_id=doctor_id,
                               claimed_amt=claimed, adjusted_amt=adjusted,
                               decided_amt=claimed - adjusted))
    await session.flush()


async def test_collection_basis(session):
    # 청구 doctor_id 없음 → 수납(본인부담) 기준
    await _payment(session, doctor_id=10, amount=200_000, paid_on=date(2026, 6, 10), no="C1")
    await _payment(session, doctor_id=10, amount=150_000, paid_on=date(2026, 6, 20), no="C2")
    out = await svc.aggregate(session, period="2026-06")
    row = next(r for r in out["rows"] if r["doctor_id"] == 10)
    assert row["basis"] == "COLLECTION" and row["collection"] == 350_000
    assert row["net_revenue"] == 350_000


async def test_claim_basis_with_deduction(session):
    # 청구 doctor_id 있음 → 발생주의(청구) − 삭감
    await _claim(session, doctor_id=20, claimed=5_000_000, adjusted=300_000,
                 care_month="2026-06", receipt="R-1")
    out = await svc.aggregate(session, period="2026-06", doctor_id=20)
    row = out["rows"][0]
    assert row["basis"] == "CLAIM" and row["claim_revenue"] == 5_000_000
    assert row["deduction"] == 300_000 and row["net_revenue"] == 4_700_000


async def test_period_filter(session):
    await _payment(session, doctor_id=30, amount=100_000, paid_on=date(2026, 5, 31), no="C3")
    await _payment(session, doctor_id=30, amount=500_000, paid_on=date(2026, 6, 1), no="C4")
    out = await svc.aggregate(session, period="2026-06", doctor_id=30)
    assert out["rows"][0]["collection"] == 500_000      # 5월 제외
