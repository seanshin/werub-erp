"""ABC 원가 배부 (FR-42) — 배부엔진 + 서비스(인건비 자동·동인 폴백)."""
from datetime import date

from erp.common.models import Department
from erp.costing.abc_service import AbcAllocationService
from erp.costing.allocation import AllocationEngine
from erp.hr.models import Employee, PayrollLine, PayrollRecord

eng = AllocationEngine()


def test_allocate_single_pool_by_driver():
    r = eng.allocate(pools=[{"name": "인건비", "amount": 1000000, "driver": "HEADCOUNT"}],
                     driver_values={"HEADCOUNT": {1: 30, 2: 10}})
    by = {o["object_id"]: o["total"] for o in r["objects"]}
    assert by[1] == 750000 and by[2] == 250000     # 30:10 비율
    assert r["grand_total"] == 1000000


def test_allocate_multi_pool():
    r = eng.allocate(
        pools=[{"name": "인건비", "amount": 1000000, "driver": "HEADCOUNT"},
               {"name": "재료비", "amount": 400000, "driver": "CASES"}],
        driver_values={"HEADCOUNT": {1: 30, 2: 10}, "CASES": {1: 1, 2: 3}})
    by = {o["object_id"]: o for o in r["objects"]}
    assert by[1]["total"] == 850000        # 인건비 750k + 재료 100k
    assert by[2]["total"] == 550000        # 250k + 300k


def test_zero_weight_pool_skipped():
    r = eng.allocate(pools=[{"name": "x", "amount": 100, "driver": "D"}],
                     driver_values={"D": {}})
    assert r["objects"] == [] and r["grand_total"] == 0


async def test_abc_service_labor_auto(session):
    d1 = Department(name="내과")
    d2 = Department(name="외과")
    session.add_all([d1, d2])
    await session.flush()
    for i, d in enumerate([d1, d1, d1, d2]):       # 내과 3명, 외과 1명
        e = Employee(employee_no=f"E{i}", name=f"n{i}", hired_on=date(2020, 1, 1),
                     department_id=d.id, job_group="NURSE")
        session.add(e)
        await session.flush()
        rec = PayrollRecord(employee_id=e.id, yyyymm="2026-05")
        session.add(rec)
        await session.flush()
        session.add(PayrollLine(record_id=rec.id, item_code="BASE",
                                item_kind="EARNING", amount=1000000))
    await session.flush()

    r = await AbcAllocationService().allocate(session, yyyymm="2026-05")
    by = {o["object_id"]: o for o in r["objects"]}
    assert by[d1.id]["total"] == 3000000 and by[d1.id]["department"] == "내과"
    assert by[d2.id]["total"] == 1000000      # 인건비 4M을 인원 3:1 배부
