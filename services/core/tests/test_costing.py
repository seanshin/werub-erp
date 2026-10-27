"""ABC 원가 (FR-50) — 진료과별 인건비·삭감손실 집계."""
from datetime import date

from erp.claims.models import ClaimBatch, ClaimStatement
from erp.common.models import Department
from erp.costing.service import CostingService
from erp.hr.models import Employee, PayrollLine, PayrollRecord

svc = CostingService()


async def _seed(session):
    d = Department(name="내과")
    session.add(d)
    await session.flush()
    e = Employee(employee_no="E1", name="김의사", hired_on=date(2020, 1, 1), department_id=d.id)
    session.add(e)
    await session.flush()
    rec = PayrollRecord(employee_id=e.id, yyyymm="2026-05")
    session.add(rec)
    await session.flush()
    session.add_all([
        PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=3000000),
        PayrollLine(record_id=rec.id, item_code="OT", item_kind="EARNING", amount=500000),
        PayrollLine(record_id=rec.id, item_code="TAX", item_kind="DEDUCTION", amount=300000),
    ])
    batch = ClaimBatch(receipt_no="R1", care_month="2026-05", notice_arrived_on=date(2026, 6, 1))
    session.add(batch)
    await session.flush()
    session.add_all([
        ClaimStatement(batch_id=batch.id, stmt_serial="1", dept_code="IM",
                       claimed_amt=1000000, decided_amt=900000),
        ClaimStatement(batch_id=batch.id, stmt_serial="2", dept_code="IM",
                       claimed_amt=500000, decided_amt=480000),
    ])
    await session.flush()
    return d.id


async def test_preauth_summary_flags_post_approval_pending(session):
    """preauth-summary — 카테고리 집계 + 응급우회 사후승인 미결 건수/유형(HIS 보완)."""
    from erp.common.router import CurrentUser
    from erp.costing.router import preauth_summary
    from erp.finance.models import PreauthOrder
    session.add_all([
        PreauthOrder(order_ref="P1", category="고가검사", amount=5000000,
                     approved_on=date(2026, 7, 10), channel="STANDARD", post_approved=True),
        PreauthOrder(order_ref="P2", category="고가검사", amount=300000, order_type="마약류",
                     approved_on=date(2026, 7, 12), channel="EMERGENCY_BYPASS", post_approved=False),
    ])
    await session.flush()
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    out = await preauth_summary(since=date(2026, 7, 1), until=date(2026, 7, 31),
                                user=admin, session=session)
    assert out["total_count"] == 2 and out["total_amount"] == 5300000
    assert out["post_approval_pending_count"] == 1
    assert out["post_approval_pending_amount"] == 300000
    assert out["post_approval_pending_by_type"] == [{"order_type": "마약류", "count": 1,
                                                     "amount": 300000}]


async def test_labor_by_department(session):
    did = await _seed(session)
    rows = await svc.labor_by_department(session, yyyymm="2026-05")
    r = next(x for x in rows if x["department_id"] == did)
    assert r["labor_cost"] == 3500000.0  # EARNING만(BASE+OT), TAX 제외
    assert r["headcount"] == 1 and r["department"] == "내과"


async def test_deduction_loss_by_department(session):
    await _seed(session)
    rows = await svc.deduction_loss_by_department(session, care_month="2026-05")
    im = next(x for x in rows if x["dept_code"] == "IM")
    assert im["claimed"] == 1500000.0 and im["decided"] == 1380000.0
    assert im["deduction_loss"] == 120000.0  # (100k+20k)


async def test_summary_totals(session):
    await _seed(session)
    s = await svc.summary(session, yyyymm="2026-05")
    assert s["labor_total"] == 3500000.0 and s["deduction_loss_total"] == 120000.0
