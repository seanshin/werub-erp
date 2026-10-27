"""ABC 원가 배부 (FR-42) — 원가풀(인건비 자동·재료/간접 입력) → 동인으로 진료과 배부.

인건비 풀=급여 자동, HEADCOUNT 동인=직원수 자동. CASES/AREA 동인값은 cost_driver_values
(원가팀 협의 D42 후 입력), 미입력 시 HEADCOUNT로 폴백. 배부 로직은 AllocationEngine.
"""
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.months import require_ym
from erp.common.models import Department
from erp.costing.allocation import AllocationEngine
from erp.finance.models import CostDriverValue
from erp.hr.models import Employee, PayrollLine, PayrollRecord


class AbcAllocationService:
    def __init__(self):
        self._engine = AllocationEngine()

    async def allocate(self, session: AsyncSession, *, yyyymm: str,
                       material_amount: float = 0, overhead_amount: float = 0) -> dict:
        labor = float(await session.scalar(
            select(func.coalesce(func.sum(
                case((PayrollLine.item_kind == "EARNING", PayrollLine.amount), else_=0)), 0))
            .select_from(PayrollLine)
            .join(PayrollRecord, PayrollRecord.id == PayrollLine.record_id)
            .where(PayrollRecord.yyyymm == require_ym(yyyymm))) or 0)

        head = {d: float(c) for d, c in (await session.execute(
            select(Employee.department_id, func.count())
            .where(Employee.resigned_on.is_(None), Employee.department_id.isnot(None))
            .group_by(Employee.department_id))).all()}
        drivers: dict = {"HEADCOUNT": head}
        for did, drv, val in (await session.execute(
            select(CostDriverValue.department_id, CostDriverValue.driver, CostDriverValue.value)
            .where(CostDriverValue.period == yyyymm))).all():
            drivers.setdefault(drv, {})[did] = float(val)
        for d in ("CASES", "AREA"):           # 동인값 미입력 시 인원수 폴백
            if not drivers.get(d):
                drivers[d] = head

        pools = [{"name": "인건비", "amount": labor, "driver": "HEADCOUNT"}]
        if material_amount:
            pools.append({"name": "재료비", "amount": float(material_amount), "driver": "CASES"})
        if overhead_amount:
            pools.append({"name": "간접비", "amount": float(overhead_amount), "driver": "AREA"})

        result = self._engine.allocate(pools=pools, driver_values=drivers)
        names = {d: n for d, n in (await session.execute(
            select(Department.id, Department.name))).all()}
        for o in result["objects"]:
            o["department"] = names.get(o["object_id"])
        result["yyyymm"] = yyyymm
        return result
