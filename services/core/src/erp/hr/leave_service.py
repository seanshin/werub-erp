"""연차 산정·휴가 워크플로우 — FR-05/FR-31.

근로기준법 제60조 (산정 룰 — 설계 G3와 별개로 법 조문이 명확한 부분):
- 1년 미만: 1개월 개근 시 1일 (최대 11일)
- 1년 이상: 15일 + 3년차부터 2년마다 1일 가산, 상한 25일
  (제60조 제4항: 가산 = (근속연수 - 1) // 2, 15 + 가산 <= 25)
"""
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ForbiddenError, ValidationError
from erp.hr.models import Employee, LeaveGrant, LeaveRequest


def annual_leave_days(hired_on: date, for_year: int) -> tuple[float, str]:
    """해당 연도 부여 연차 일수 + 산정 근거 (감사 추적용)."""
    jan1 = date(for_year, 1, 1)
    service_years = (jan1 - hired_on).days // 365  # 연도 기준 단순 근속 (회계연도 방식)

    if service_years < 1:
        # 1년 미만 — 월 개근 1일 (입사월부터 연말까지, 최대 11)
        months = min(11, max(0, (12 - hired_on.month) if hired_on.year == for_year else 11))
        return float(months), f"근로기준법 §60②: 1년 미만, 개근 월 {months}일"

    bonus = min((service_years - 1) // 2, 10)  # 15+10=25 상한
    days = min(15 + bonus, 25)
    return float(days), f"근로기준법 §60①④: 근속 {service_years}년 = 15 + 가산 {bonus}"


class LeaveService:
    async def grant_annual(self, session: AsyncSession, *, employee_id: int,
                           year: int) -> LeaveGrant:
        """연차 자동 부여 (FR-31) — 연 1회, 멱등(uq_leave_grant_year)."""
        existing = (await session.execute(
            select(LeaveGrant).where(LeaveGrant.employee_id == employee_id,
                                     LeaveGrant.grant_year == year)
        )).scalar_one_or_none()
        if existing is not None:
            return existing
        emp = (await session.execute(
            select(Employee).where(Employee.id == employee_id))).scalar_one_or_none()
        if emp is None:
            raise ValidationError("존재하지 않는 직원입니다")
        days, basis = annual_leave_days(emp.hired_on, year)
        grant = LeaveGrant(employee_id=employee_id, grant_year=year,
                           granted_days=days, basis=basis)
        session.add(grant)
        return grant

    async def grant_annual_all(self, session: AsyncSession, *, year: int) -> dict:
        """재직 전원 연차 자동 부여 (연 1회·신규입사 스윕) — 멱등(uq_leave_grant_year).

        `grant_annual`이 어디서도 호출되지 않아 LeaveGrant가 없으면 remaining_days=0 → 모든 연차
        신청이 거부되던 결함 해소. 재직자(resigned_on IS NULL) 전원에 당해연도 부여. 기부여분은 스킵.
        """
        # 미부여 재직자만 단일 쿼리로 추림(N+1 회피·스케줄러 상시 실행 저비용)
        granted_sub = (select(LeaveGrant.employee_id)
                       .where(LeaveGrant.grant_year == year)).scalar_subquery()
        pending = (await session.scalars(
            select(Employee.id).where(Employee.resigned_on.is_(None),
                                      Employee.id.notin_(granted_sub)))).all()
        for eid in pending:
            await self.grant_annual(session, employee_id=eid, year=year)
        return {"year": year, "granted": len(pending)}

    async def remaining_days(self, session: AsyncSession, *, employee_id: int,
                             year: int) -> float:
        granted = (await session.execute(
            select(func.coalesce(func.sum(LeaveGrant.granted_days), 0))
            .where(LeaveGrant.employee_id == employee_id, LeaveGrant.grant_year == year)
        )).scalar_one()
        used = (await session.execute(
            select(func.coalesce(func.sum(LeaveRequest.days), 0))
            .where(LeaveRequest.employee_id == employee_id,
                   LeaveRequest.status == "APPROVED",
                   LeaveRequest.leave_type == "ANNUAL",
                   func.substr(func.cast(LeaveRequest.start_on, __import__("sqlalchemy").String), 1, 4)
                   == str(year))
        )).scalar_one()
        return float(granted) - float(used)

    async def request(self, session: AsyncSession, *, employee_id: int, leave_type: str,
                      start_on: date, end_on: date, days: float,
                      reason: str | None = None) -> LeaveRequest:
        if end_on < start_on:
            raise ValidationError("종료일이 시작일보다 빠릅니다")
        if days <= 0:   # 감사 M6 — 음수/0 일수 차단(음수 연차로 잔여 인플레·무의미 레코드 방지)
            raise ValidationError("신청 일수는 0보다 커야 합니다", {"days": days})
        if leave_type == "ANNUAL":
            remaining = await self.remaining_days(session, employee_id=employee_id,
                                                  year=start_on.year)
            if days > remaining:
                raise ValidationError("잔여 연차가 부족합니다",
                                      {"remaining": remaining, "requested": days})
        req = LeaveRequest(employee_id=employee_id, leave_type=leave_type,
                           start_on=start_on, end_on=end_on, days=days, reason=reason)
        session.add(req)
        await session.flush()  # id 할당 (후속 decide 참조)
        return req

    async def decide(self, session: AsyncSession, *, request_id: int, approve: bool,
                     decider_user_id: int, decider_dept_id: int | None,
                     decider_scope_dept_only: bool) -> LeaveRequest:
        """승인/반려 — 부서장은 본부서만·직무분리(본인 신청 불가)·승인 시 잔량 재검증(행잠금)."""
        req = (await session.execute(
            select(LeaveRequest).where(LeaveRequest.id == request_id).with_for_update()
        )).scalar_one_or_none()
        if req is None:
            raise ValidationError("존재하지 않는 신청입니다")
        if req.status != "REQUESTED":
            raise ValidationError("이미 처리된 신청입니다", {"status": req.status})
        if decider_scope_dept_only:
            emp = (await session.execute(
                select(Employee).where(Employee.id == req.employee_id))).scalar_one()
            if emp.department_id != decider_dept_id:
                raise ForbiddenError("본인 부서의 신청만 처리할 수 있습니다")
        # 감사 M4 — 직무분리: 결재자가 신청 당사자면 자기결재 차단(원장 전권도 예외 없음)
        from erp.common.models import User
        decider_emp = await session.scalar(
            select(User.employee_id).where(User.id == decider_user_id))
        if decider_emp is not None and decider_emp == req.employee_id:
            raise ForbiddenError("직무분리 — 본인 휴가 신청은 승인할 수 없습니다")
        # 감사 H1 — 승인 시 잔량 재검증(다중 신청 개별통과 후 초과 승인 방지). 이미 APPROVED된
        # 사용분을 반영해 남은 한도로 재판정(행잠금으로 동시 승인 직렬화).
        if approve and req.leave_type == "ANNUAL":
            remaining = await self.remaining_days(session, employee_id=req.employee_id,
                                                  year=req.start_on.year)
            if float(req.days) > remaining:
                raise ValidationError("잔여 연차 초과 — 승인 불가",
                                      {"remaining": remaining, "requested": float(req.days)})
        req.status = "APPROVED" if approve else "REJECTED"
        req.decided_by = decider_user_id
        req.decided_at = datetime.now(timezone.utc)
        return req
