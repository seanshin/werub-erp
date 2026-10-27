"""세무 신고 집계 (FR-07 확장) — 원천징수이행상황·4대보험 공단신고."""
from datetime import date, datetime, timezone

from erp.hr.models import Employee, PayrollLine, PayrollRecord
from erp.tax.service import TaxService

svc = TaxService()


async def _seed(session, n=2):
    for i in range(n):
        e = Employee(employee_no=f"T{i}", name=f"직원{i}", hired_on=date(2020, 1, 1),
                     department_id=1, job_group="NURSE")
        session.add(e)
        await session.flush()
        # 집계는 확정 급여만(2026-10-09 전체 흐름 검토 C) — 시드도 확정 상태로
        rec = PayrollRecord(employee_id=e.id, yyyymm="2026-05", confirmed_at=datetime(2026, 5, 25, tzinfo=timezone.utc))
        session.add(rec)
        await session.flush()
        session.add_all([
            PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=3000000),
            PayrollLine(record_id=rec.id, item_code="TAX", item_kind="DEDUCTION", amount=100000),
        ])
    await session.flush()


async def test_withholding(session):
    await _seed(session, 2)
    w = await svc.withholding(session, yyyymm="2026-05")
    assert w["인원"] == 2
    assert w["총지급액"] == 6000000.0           # 3M × 2
    assert w["원천징수세액"] == 200000.0        # TAX 100k × 2


async def test_withholding_includes_incentive_tax(session):
    """심층 재검토 — 성과급 원천징수(INCTAX)도 원천징수세액에 집계(총지급액에 성과급 포함되므로)."""
    e = Employee(employee_no="TW", name="성과직원", hired_on=date(2020, 1, 1),
                 department_id=1, job_group="DOCTOR")
    session.add(e)
    await session.flush()
    rec = PayrollRecord(employee_id=e.id, yyyymm="2026-05", confirmed_at=datetime(2026, 5, 25, tzinfo=timezone.utc))
    session.add(rec)
    await session.flush()
    session.add_all([
        PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=5000000),
        PayrollLine(record_id=rec.id, item_code="TAX", item_kind="DEDUCTION", amount=200000),
        PayrollLine(record_id=rec.id, item_code="INCENTIVE", item_kind="EARNING", amount=1000000),
        PayrollLine(record_id=rec.id, item_code="INCTAX", item_kind="DEDUCTION", amount=33000),
    ])
    await session.flush()
    w = await svc.withholding(session, yyyymm="2026-05")
    assert w["총지급액"] == 6000000.0            # BASE 5M + 성과급 1M
    assert w["원천징수세액"] == 233000.0         # TAX 200k + INCTAX 33k (INCTAX 누락 시 200k)


async def test_insurance_filing(session):
    await _seed(session, 2)
    f = await svc.insurance_filing(session, yyyymm="2026-05")
    assert f["인원"] == 2
    # 보수월액 3M 1인당 4대보험 합계 291,520(2026 고시) × 2
    assert f["근로자부담"]["합계"] == 583040
    assert f["근로자부담"]["국민연금"] == 285000        # 142,500 × 2
    assert f["공단납부_추정_노사합산"]["국민연금"] == 570000  # 노사 50:50


# ── 부가세 신고 집계 보조 (P2-⑦) ─────────────────────────────
async def test_vat_summary(session):
    """전자세금계산서 매출·매입 세액 집계 — 면세/과세 구분·기간 필터·납부추정."""
    from erp.finance.models import TaxInvoice
    session.add_all([
        TaxInvoice(approval_no="V-1", invoice_type="TAX", direction="OUT",
                   issue_date=date(2026, 6, 5), supplier_bizno="111",
                   supply_amount=1_000_000, vat_amount=100_000, total_amount=1_100_000),
        TaxInvoice(approval_no="V-2", invoice_type="TAX", direction="IN",
                   issue_date=date(2026, 6, 6), supplier_bizno="222",
                   supply_amount=500_000, vat_amount=50_000, total_amount=550_000),
        TaxInvoice(approval_no="V-3", invoice_type="EXEMPT", direction="OUT",
                   issue_date=date(2026, 6, 7), supplier_bizno="333",
                   supply_amount=2_000_000, vat_amount=0, total_amount=2_000_000),
        TaxInvoice(approval_no="V-4", invoice_type="TAX", direction="IN",
                   issue_date=date(2026, 5, 1), supplier_bizno="444",   # 기간 밖(5월)
                   supply_amount=999, vat_amount=99, total_amount=1_098),
        TaxInvoice(approval_no="V-5", invoice_type="TAX", direction="OUT",
                   issue_date=date(2026, 6, 9), supplier_bizno="555", status="VOID",  # 무효
                   supply_amount=777, vat_amount=77, total_amount=854),
    ])
    await session.flush()
    r = await svc.vat_summary(session, start=date(2026, 6, 1), end=date(2026, 6, 30))
    assert r["output_vat"] == 100_000 and r["input_vat"] == 50_000   # VOID·기간밖 제외
    assert r["payable_estimate"] == 50_000
    assert r["sales"]["taxable"]["supply"] == 1_000_000
    assert r["sales"]["exempt"]["supply"] == 2_000_000               # 의료용역 면세
    assert r["purchases"]["taxable"]["vat"] == 50_000
