"""ABC 원가 (FR-50) — 진료과 차원 원가 집계. 1차: 인건비·삭감손실(직접 귀속 가능분).

읽기 전용 — 기존 데이터(급여·청구) 집계. 재료비 배부·행위(activity) 차원·원가동인은
후속(원가동인 정의·dept_code↔department 매핑 확정 후). design §3.x costing 모듈.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.months import require_ym
from erp.claims.models import ClaimBatch, ClaimStatement
from erp.common.models import Department
from erp.hr.models import Employee, PayrollLine, PayrollRecord


class CostingService:
    async def labor_by_department(self, session: AsyncSession, *, yyyymm: str) -> list[dict]:
        """인건비(급여 EARNING) 진료과별 집계 + 인원."""
        rows = (await session.execute(
            select(Employee.department_id,
                   func.coalesce(func.sum(PayrollLine.amount), 0),
                   func.count(func.distinct(Employee.id)))
            .select_from(PayrollLine)
            .join(PayrollRecord, PayrollRecord.id == PayrollLine.record_id)
            .join(Employee, Employee.id == PayrollRecord.employee_id)
            .where(PayrollRecord.yyyymm == require_ym(yyyymm), PayrollLine.item_kind == "EARNING")
            .group_by(Employee.department_id))).all()
        names = {d: n for d, n in (await session.execute(
            select(Department.id, Department.name))).all()}
        return [{"department_id": did, "department": names.get(did),
                 "labor_cost": float(labor), "headcount": int(hc)}
                for did, labor, hc in rows]

    async def deduction_loss_by_department(self, session: AsyncSession, *,
                                           care_month: str) -> list[dict]:
        """삭감 손실(청구−결정) 진료과(dept_code)별 집계."""
        care_month = require_ym(care_month)
        rows = (await session.execute(
            select(ClaimStatement.dept_code,
                   func.coalesce(func.sum(ClaimStatement.claimed_amt), 0),
                   func.coalesce(func.sum(ClaimStatement.decided_amt), 0))
            .join(ClaimBatch, ClaimBatch.id == ClaimStatement.batch_id)
            .where(ClaimBatch.care_month == care_month)
            .group_by(ClaimStatement.dept_code))).all()
        return [{"dept_code": dc, "claimed": float(c), "decided": float(d),
                 "deduction_loss": float(c) - float(d)} for dc, c, d in rows]

    async def statement_lines_by_department(self, session: AsyncSession, *, care_month: str,
                                            dept_code: str, limit: int = 200) -> list[dict]:
        """진료과(dept_code) 명세서 라인(2단계 드릴) — 청구·결정·조정·삭감(삭감순).

        명세서 일련번호·금액만(차트번호 등 PII 제외). analytics는 본 메서드를 경유(경계 계약).
        """
        ded = (func.coalesce(ClaimStatement.claimed_amt, 0)
               - func.coalesce(ClaimStatement.decided_amt, 0))
        rows = (await session.execute(
            select(ClaimStatement.stmt_serial, ClaimStatement.claimed_amt,
                   ClaimStatement.decided_amt, ClaimStatement.adjusted_amt)
            .join(ClaimBatch, ClaimBatch.id == ClaimStatement.batch_id)
            .where(ClaimBatch.care_month == care_month, ClaimStatement.dept_code == dept_code)
            .order_by(ded.desc()).limit(min(limit, 500)))).all()
        return [{"stmt_serial": s, "claimed": float(c or 0), "decided": float(d or 0),
                 "adjusted": float(a or 0), "deduction": float((c or 0) - (d or 0))}
                for s, c, d, a in rows]

    async def summary(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """기간 원가 구성 요약(총계)."""
        labor = sum(r["labor_cost"] for r in await self.labor_by_department(session, yyyymm=yyyymm))
        loss = sum(r["deduction_loss"]
                   for r in await self.deduction_loss_by_department(session, care_month=yyyymm))
        return {"yyyymm": yyyymm, "labor_total": labor, "deduction_loss_total": loss}

    async def department_baseline(self, session: AsyncSession, *, yyyymm: str,
                                  months: int = 6) -> list[dict]:
        """진료과별 시뮬레이션 기준선 — 최근 N개월 월평균 수익·원가·손익(dept_code별).

        보험 결정수익·삭감손실은 진료월(care_month) 윈도우 합/개월수, 배부원가는 동일 윈도우
        스냅샷 합/개월수. 프론트가 성장률 드라이버를 곱해 진료과별 예상 손익 투영.
        """
        from erp.finance.models import CostAllocationSnapshot
        y, m = int(yyyymm[:4]), int(yyyymm[5:7])
        win = []
        for k in range(months):
            mm = m - 1 - k
            yy = y + (mm // 12)
            win.append(f"{yy}-{(mm % 12) + 1:02d}")
        rev_rows = (await session.execute(
            select(ClaimStatement.dept_code,
                   func.coalesce(func.sum(ClaimStatement.decided_amt), 0).label("revenue"),
                   func.coalesce(func.sum(ClaimStatement.claimed_amt - ClaimStatement.decided_amt),
                                 0).label("loss"))
            .join(ClaimBatch, ClaimBatch.id == ClaimStatement.batch_id)
            .where(ClaimBatch.care_month.in_(win))
            .group_by(ClaimStatement.dept_code))).all()
        code_to_dept = {c: (i, n) for i, n, c in (await session.execute(
            select(Department.id, Department.name, Department.code)
            .where(Department.code.isnot(None)))).all()}
        cost_rows = (await session.execute(
            select(CostAllocationSnapshot.department_id,
                   func.coalesce(func.sum(CostAllocationSnapshot.total_cost), 0))
            .where(CostAllocationSnapshot.period.in_(win))
            .group_by(CostAllocationSnapshot.department_id))).all()
        cost_by_did = {did: float(c) for did, c in cost_rows}
        # 원가 스냅샷은 마감 시점에만 적재 → 윈도우 내 실제 스냅샷 개월수로 나눠야
        # 월평균 원가가 과소(→손익 과대) 되지 않음(수익은 윈도우 길이 기준 유지).
        cost_div = (await session.execute(
            select(func.count(func.distinct(CostAllocationSnapshot.period)))
            .where(CostAllocationSnapshot.period.in_(win)))).scalar() or months
        out = []
        for dc, revenue, loss in rev_rows:
            dept = code_to_dept.get(dc)
            did, name = dept if dept else (None, dc)
            rev = float(revenue) / months
            cost = cost_by_did.get(did, 0.0) / cost_div
            ded = float(loss) / months
            profit = rev - cost - ded
            out.append({"dept_code": dc, "department": name,
                        "avg_revenue": round(rev), "avg_cost": round(cost),
                        "avg_deduction": round(ded), "avg_profit": round(profit),
                        "margin": round(profit / rev * 100, 1) if rev else None})
        return sorted(out, key=lambda x: -x["avg_revenue"])


class CostSnapshotService:
    """원가 배부 스냅샷·진료과 손익 (FR-50 확장)."""

    async def save_snapshot(self, session: AsyncSession, *, yyyymm: str,
                            material_amount: float = 0, overhead_amount: float = 0,
                            user_id: int | None = None) -> dict:
        """ABC 배부를 실행해 부서별 원가를 스냅샷(마감 기준)으로 보존. 재실행 시 갱신."""
        yyyymm = require_ym(yyyymm)
        from sqlalchemy import delete

        from erp.costing.abc_service import AbcAllocationService
        from erp.finance.models import CostAllocationSnapshot
        result = await AbcAllocationService().allocate(
            session, yyyymm=yyyymm, material_amount=material_amount, overhead_amount=overhead_amount)
        await session.execute(delete(CostAllocationSnapshot).where(
            CostAllocationSnapshot.period == yyyymm))
        total = 0.0
        for o in result["objects"]:
            pools = o.get("pools", {})          # {pool_name: amount} dict
            tc = float(o.get("total", 0))
            total += tc
            session.add(CostAllocationSnapshot(
                period=yyyymm, department_id=o["object_id"], department=o.get("department"),
                labor=pools.get("인건비", 0), material=pools.get("재료비", 0),
                overhead=pools.get("간접비", 0), total_cost=tc, created_by=user_id))
        # 배부 정합: 배부액 합 == 입력 원가풀 합(인건비+재료+간접). 동인 총가중치 0인 풀은
        # 엔진이 통째로 드롭(배부 대상 없음)하므로, 배부액과 입력 풀 합을 비교해야 누락을 검출한다.
        # (과거 comp_total은 total과 동일 식이라 항상 True인 가짜 검증이었음.)
        allocated = float(result.get("grand_total", total))
        input_total = sum(float(p["amount"]) for p in result.get("pools", []))
        tol = len(result["objects"]) + 1                    # 부서별 반올림 잔차 허용(±1원/부서)
        return {"period": yyyymm, "departments": len(result["objects"]),
                "total_allocated": round(allocated), "input_total": round(input_total),
                "reconciled": abs(allocated - input_total) <= tol}

    async def snapshots(self, session: AsyncSession, *, yyyymm: str) -> list[dict]:
        from erp.finance.models import CostAllocationSnapshot
        rows = (await session.execute(select(CostAllocationSnapshot).where(
            CostAllocationSnapshot.period == yyyymm)
            .order_by(CostAllocationSnapshot.total_cost.desc()))).scalars().all()
        return [{"department_id": s.department_id, "department": s.department,
                 "labor": float(s.labor), "material": float(s.material),
                 "overhead": float(s.overhead), "total_cost": float(s.total_cost)} for s in rows]

    async def department_pl(self, session: AsyncSession, *, yyyymm: str) -> list[dict]:
        """진료과별 손익 — 보험 결정수익(claims dept_code→department.code) − 배부원가 − 삭감손실."""
        from erp.finance.models import CostAllocationSnapshot
        # 보험 결정수익·삭감손실 by dept_code (진료월 care_month 기준)
        rev_rows = (await session.execute(
            select(ClaimStatement.dept_code,
                   func.coalesce(func.sum(ClaimStatement.decided_amt), 0).label("revenue"),
                   func.coalesce(func.sum(ClaimStatement.claimed_amt - ClaimStatement.decided_amt),
                                 0).label("loss"))
            .join(ClaimBatch, ClaimBatch.id == ClaimStatement.batch_id)
            .where(ClaimBatch.care_month == yyyymm)
            .group_by(ClaimStatement.dept_code))).all()
        code_to_dept = {c: (i, n) for i, n, c in (await session.execute(
            select(Department.id, Department.name, Department.code)
            .where(Department.code.isnot(None)))).all()}
        cost_map = {s.department_id: s for s in (await session.execute(
            select(CostAllocationSnapshot).where(CostAllocationSnapshot.period == yyyymm))).scalars()}
        out = []
        for dc, revenue, loss in rev_rows:
            dept = code_to_dept.get(dc)
            did, name = dept if dept else (None, dc)
            cost = float(cost_map[did].total_cost) if did in cost_map else 0.0
            rev = float(revenue)
            profit = rev - cost - float(loss)
            out.append({"dept_code": dc, "department": name, "department_id": did,
                        "revenue": rev, "allocated_cost": cost, "deduction_loss": float(loss),
                        "profit": profit, "margin": round(profit / rev * 100, 1) if rev else None})
        return sorted(out, key=lambda x: x["profit"])

    async def multistage(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """2단계 원가배부 — 지원부서(코드없음) 원가를 진료부서(code 보유)에 인원수 기준 배부.

        스냅샷(직접원가) 선저장 필요. 결과: 진료부서별 직접+배부지원=총원가.
        """
        from erp.common.models import Department
        from erp.finance.models import CostAllocationSnapshot
        from erp.hr.models import Employee
        snaps = (await session.execute(select(CostAllocationSnapshot).where(
            CostAllocationSnapshot.period == yyyymm))).scalars().all()
        if not snaps:
            return {"period": yyyymm, "items": [], "support_total": 0, "note": "스냅샷 미저장"}
        codes = {d: c for d, c in (await session.execute(
            select(Department.id, Department.code))).all()}
        head = {d: int(c) for d, c in (await session.execute(
            select(Employee.department_id, func.count(func.distinct(Employee.id)))
            .where(Employee.resigned_on.is_(None), Employee.department_id.isnot(None))
            .group_by(Employee.department_id))).all()}
        # 지원(코드 없음) vs 진료(코드 보유)
        support_total = sum(float(s.total_cost) for s in snaps if not codes.get(s.department_id))
        rev = [s for s in snaps if codes.get(s.department_id)]
        rev_head_total = sum(head.get(s.department_id, 0) for s in rev)
        items = []
        for s in rev:
            h = head.get(s.department_id, 0)
            # 전 진료부서 인원 0이면 배부 불능 — 종전 `or 1` 폴백은 지원원가 전량을 조용히
            # 소실(총액보존 위반 — 계산검토 M-3). 미배부액을 명시 반환으로 가시화.
            allocated = round(support_total * h / rev_head_total) if rev_head_total else 0
            direct = float(s.total_cost)
            items.append({"department_id": s.department_id, "department": s.department,
                          "code": codes.get(s.department_id), "direct_cost": direct,
                          "allocated_support": allocated, "total_cost": direct + allocated,
                          "headcount": h})
        allocated_total = sum(i["allocated_support"] for i in items)
        unallocated = round(support_total) - allocated_total
        return {"period": yyyymm, "support_total": round(support_total),
                "allocated_total": allocated_total,
                "unallocated_support": unallocated,      # 라운딩 잔차 or 인원 0 미배부(가시화)
                "reconciled": abs(unallocated) <= max(1, len(items)),
                "items": sorted(items, key=lambda x: -x["total_cost"])}
