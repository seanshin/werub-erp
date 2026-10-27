"""4대보험 취득·상실 신고 추적 (인사 P1-③).

사유발생(입사·퇴사·휴직·복직) 시 자동 적재 → 담당자가 공단 신고 후 처리 마킹.
기한: 국민연금·건강보험 14일 이내, 고용·산재 다음달 15일 — due_on은 보수적으로
사유발생일+14일 단일 적용(빠른 기한), note에 보험별 기한 명시.
"""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.hr.models import Employee, InsuranceReportEvent

_DUE_DAYS = 14
_EVENT_NOTE = {
    "ACQUIRE": "취득 — 국민·건보 14일 이내, 고용·산재 다음달 15일",
    "LOSE": "상실 — 국민·건보 14일 이내(상실일=퇴사 다음날), 고용·산재 다음달 15일",
    "EXEMPT": "납부예외(휴직) — 국민연금 납부예외 신청·건보 납부고지 유예",
    "RESUME": "납부재개(복직) — 국민연금 납부재개·건보 유예 해제",
}


class InsuranceReportService:
    async def record(self, session: AsyncSession, *, employee_id: int, event: str,
                     basis_on: date, note: str | None = None) -> InsuranceReportEvent | None:
        """사유발생 적재 — (직원, 이벤트, 사유일) 멱등(중복 무시·None 반환)."""
        existing = await session.scalar(
            select(InsuranceReportEvent).where(
                InsuranceReportEvent.employee_id == employee_id,
                InsuranceReportEvent.event == event,
                InsuranceReportEvent.basis_on == basis_on))
        if existing is not None:
            return None
        ev = InsuranceReportEvent(
            employee_id=employee_id, event=event, basis_on=basis_on,
            due_on=basis_on + timedelta(days=_DUE_DAYS),
            note=note or _EVENT_NOTE.get(event))
        try:
            # 동시성 UNIQUE 경합 — 자체 savepoint만 롤백(C1: session.rollback()은
            # 호출측 savepoint 격리(inbound consume 등)를 깨고 배치 전체를 오염시킴)
            async with session.begin_nested():
                session.add(ev)
                await session.flush()
        except IntegrityError:
            return None
        return ev

    async def pending(self, session: AsyncSession, *, status: str = "PENDING",
                      today: date | None = None) -> list[dict]:
        rows = (await session.execute(
            select(InsuranceReportEvent, Employee.employee_no, Employee.name)
            .join(Employee, Employee.id == InsuranceReportEvent.employee_id)
            .where(InsuranceReportEvent.status == status)
            .order_by(InsuranceReportEvent.due_on))).all()
        return [{"id": ev.id, "employee_id": ev.employee_id, "employee_no": no,
                 "name": name, "event": ev.event, "basis_on": ev.basis_on,
                 "due_on": ev.due_on, "status": ev.status,
                 "overdue": bool(today and ev.status == "PENDING" and ev.due_on < today),
                 "reported_on": ev.reported_on, "note": ev.note}
                for ev, no, name in rows]

    async def mark_reported(self, session: AsyncSession, *, actor: int, event_id: int,
                            reported_on: date) -> InsuranceReportEvent:
        ev = await session.get(InsuranceReportEvent, event_id)
        if ev is None:
            raise ValidationError("존재하지 않는 신고 이벤트입니다")
        if ev.status == "REPORTED":
            raise ValidationError("이미 신고 처리된 건입니다")
        ev.status, ev.reported_on, ev.reported_by = "REPORTED", reported_on, actor
        return ev
