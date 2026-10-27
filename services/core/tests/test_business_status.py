"""사업장현황신고 집계·의료비 자료 (A-2) — 수익/매입/인건비·환자별 수납."""
from datetime import date, datetime

from erp.finance.models import Account, Voucher, VoucherLine
from erp.hr.models import Employee, PayrollLine, PayrollRecord
from erp.patient.models import Patient, Payment, Reception
from erp.patient.service import MedicalExpenseReportService
from erp.tax.service import TaxService

svc = TaxService()
_med = MedicalExpenseReportService()   # 의료비 자료는 patient 모듈로 이관(Phase0)


async def test_business_status_aggregates(session):
    session.add(Account(code="4110", name="외래수익", kind="REVENUE"))
    v = Voucher(voucher_on=date(2026, 3, 5), source_module="t", source_ref="t1")
    session.add(v)
    await session.flush()
    session.add_all([
        VoucherLine(voucher_id=v.id, account_code="4110", side="CREDIT", amount=500_000),
        VoucherLine(voucher_id=v.id, account_code="4110", side="DEBIT", amount=50_000),
    ])
    emp = Employee(employee_no="E-TX", name="세무직", hired_on=date(2025, 1, 1),
                   job_group="STAFF", employment_type="REGULAR", source="ERP")
    session.add(emp)
    await session.flush()
    rec = PayrollRecord(employee_id=emp.id, yyyymm="2026-03",
                        confirmed_at=datetime(2026, 4, 1), confirmed_by=1)
    session.add(rec)
    await session.flush()
    session.add(PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING",
                            amount=3_000_000))
    await session.flush()

    out = await svc.business_status(session, year=2026)
    assert out["revenue"]["total"] == 450_000          # 대변−차변
    assert out["revenue"]["items"][0]["month"] == "2026-03"
    assert out["payroll_total"] == 3_000_000
    assert "purchases" in out and "unmatched" in out["purchases"]


async def test_medical_expenses_per_patient(session):
    pat = Patient(chart_no="C-MED", name="김환자", birth_date=date(1980, 1, 1))
    session.add(pat)
    await session.flush()
    rcp = Reception(patient_id=pat.id, dept_code="IM", received_on=date(2026, 2, 1))
    session.add(rcp)
    await session.flush()
    # 취소 모델 = 원결제를 CANCELED로 플립 + 정정액을 신규 PAID로 추가(별도 음수행 없음).
    session.add_all([
        Payment(reception_id=rcp.id, amount=100_000, paid_on=date(2026, 2, 1),
                status="CANCELED"),                     # 원결제(취소로 플립)
        Payment(reception_id=rcp.id, amount=80_000, paid_on=date(2026, 2, 1)),   # 정정 신규 PAID
        Payment(reception_id=rcp.id, amount=999_999, paid_on=date(2025, 12, 31)),  # 타연도
    ])
    await session.flush()

    out = await _med.annual_by_patient(session, year=2026)
    assert out["count"] == 1
    assert out["items"][0]["chart_no"] == "C-MED"
    # PAID만 합산(80,000) — 취소된 원결제는 제외(음수 차감 아님·이중차감이면 −20,000으로 탈락했음)
    assert out["items"][0]["total_paid"] == 80_000
