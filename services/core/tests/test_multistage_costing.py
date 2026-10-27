"""2단계 원가배부 — 지원부서→진료부서 인원수 배부."""
from erp.common.models import Department
from erp.costing.service import CostSnapshotService
from erp.finance.models import CostAllocationSnapshot
from erp.hr.models import Employee

svc = CostSnapshotService()


async def test_support_cost_allocated_to_revenue_depts(session):
    # 진료부서 2(내과 code=IM)·3(외과 code=GS), 지원부서 9(행정 code=None)
    session.add_all([
        Department(id=2, name="내과", code="IM"),
        Department(id=3, name="외과", code="GS"),
        Department(id=9, name="행정부", code=None)])
    await session.flush()
    # 인원: 내과 6, 외과 4 (지원배부 6:4)
    for i in range(6):
        session.add(Employee(employee_no=f"IM{i}", name="x", hired_on=__import__("datetime").date(2020, 1, 1), department_id=2))
    for i in range(4):
        session.add(Employee(employee_no=f"GS{i}", name="y", hired_on=__import__("datetime").date(2020, 1, 1), department_id=3))
    # 스냅샷: 내과 직접 600, 외과 400, 행정(지원) 1000
    session.add_all([
        CostAllocationSnapshot(period="2026-05", department_id=2, department="내과", total_cost=600),
        CostAllocationSnapshot(period="2026-05", department_id=3, department="외과", total_cost=400),
        CostAllocationSnapshot(period="2026-05", department_id=9, department="행정부", total_cost=1000)])
    await session.flush()
    out = await svc.multistage(session, yyyymm="2026-05")
    assert out["support_total"] == 1000
    im = next(x for x in out["items"] if x["department_id"] == 2)
    gs = next(x for x in out["items"] if x["department_id"] == 3)
    # 지원 1000을 6:4 배부 → 내과 +600, 외과 +400
    assert im["allocated_support"] == 600 and im["total_cost"] == 1200
    assert gs["allocated_support"] == 400 and gs["total_cost"] == 800
