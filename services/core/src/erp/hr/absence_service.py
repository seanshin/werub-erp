"""휴직/복직 + 퇴사 처리 (인사 P1-①).

휴직: 발령(LEAVE) 기록 + 기간 관리 — 급여 일괄생성 제외·4대보험 납부예외 식별.
복직: return_on 확정 + 발령(REINSTATE).
퇴사: resigned_on 설정 + 발령(RESIGN) — 기존엔 설정 경로 자체가 없던 공백.
SoT 가드: HIS 관리 직원(의료진·정규직)의 퇴사는 HIS에서 — ERP는 휴직만 기록 가능.
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ForbiddenError, ValidationError
from erp.hr.insurance_report_service import InsuranceReportService
from erp.hr.models import Appointment, Employee, LeaveAbsence

_audit = AuditService()
_ins = InsuranceReportService()


class AbsenceService:
    async def start(self, session: AsyncSession, *, actor: int, employee_id: int,
                    kind: str, start_on: date, expected_return_on: date | None = None,
                    insurance_exempt: bool = True, note: str | None = None) -> LeaveAbsence:
        emp = await session.get(Employee, employee_id)
        if emp is None:
            raise ValidationError("존재하지 않는 직원입니다")
        if emp.resigned_on is not None:
            raise ValidationError("퇴직자는 휴직 처리할 수 없습니다")
        active = await self.active_absence(session, employee_id=employee_id)
        if active is not None:
            raise ValidationError("이미 휴직 중인 직원입니다 — 복직 처리 후 등록하세요")
        if expected_return_on and expected_return_on <= start_on:
            raise ValidationError("복직 예정일은 휴직 시작일 이후여야 합니다")

        absence = LeaveAbsence(
            employee_id=employee_id, kind=kind, start_on=start_on,
            expected_return_on=expected_return_on, insurance_exempt=insurance_exempt,
            note=note, created_by=actor)
        session.add(absence)
        session.add(Appointment(employee_id=employee_id, appointment_type="LEAVE",
                                effective_on=start_on, note=f"{kind} 휴직"))
        await session.flush()
        if insurance_exempt:        # 4대보험 납부예외 신고 추적 (P1-③)
            await _ins.record(session, employee_id=employee_id, event="EXEMPT",
                              basis_on=start_on)
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="hr.leave_absences", target_id=str(absence.id),
                                  after={"kind": kind, "start_on": str(start_on)})
        return absence

    async def end(self, session: AsyncSession, *, actor: int, absence_id: int,
                  return_on: date) -> LeaveAbsence:
        absence = await session.get(LeaveAbsence, absence_id)
        if absence is None:
            raise ValidationError("존재하지 않는 휴직 건입니다")
        if absence.return_on is not None:
            raise ValidationError("이미 복직 처리된 건입니다")
        if return_on <= absence.start_on:
            raise ValidationError("복직일은 휴직 시작일 이후여야 합니다")
        absence.return_on = return_on
        session.add(Appointment(employee_id=absence.employee_id,
                                appointment_type="REINSTATE", effective_on=return_on,
                                note=f"{absence.kind} 복직"))
        if absence.insurance_exempt:    # 납부재개 신고 추적 (P1-③)
            await _ins.record(session, employee_id=absence.employee_id, event="RESUME",
                              basis_on=return_on)
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="hr.leave_absences", target_id=str(absence.id),
                                  after={"return_on": str(return_on)})
        return absence

    async def active_absence(self, session: AsyncSession, *, employee_id: int,
                             on: date | None = None) -> LeaveAbsence | None:
        """기준일 현재 휴직 중인 건 — return_on 미확정이거나 기준일이 기간 내."""
        q = select(LeaveAbsence).where(LeaveAbsence.employee_id == employee_id,
                                       LeaveAbsence.return_on.is_(None))
        if on is not None:
            q = select(LeaveAbsence).where(
                LeaveAbsence.employee_id == employee_id,
                LeaveAbsence.start_on <= on,
                (LeaveAbsence.return_on.is_(None)) | (LeaveAbsence.return_on > on))
        return (await session.execute(q.order_by(LeaveAbsence.start_on.desc())
                                      .limit(1))).scalar_one_or_none()

    async def on_leave_employee_ids(self, session: AsyncSession, *, on: date) -> set[int]:
        """기준일 휴직 중 직원 id 집합 — 급여 일괄생성 제외 판정용."""
        rows = (await session.execute(
            select(LeaveAbsence.employee_id).where(
                LeaveAbsence.start_on <= on,
                (LeaveAbsence.return_on.is_(None)) | (LeaveAbsence.return_on > on))
        )).scalars().all()
        return set(rows)

    async def list(self, session: AsyncSession, *, active_only: bool = False,
                   dept_id: int | None = None, employee_id: int | None = None) -> list[dict]:
        q = (select(LeaveAbsence, Employee.employee_no, Employee.name)
             .join(Employee, Employee.id == LeaveAbsence.employee_id)
             .order_by(LeaveAbsence.start_on.desc()))
        if active_only:
            q = q.where(LeaveAbsence.return_on.is_(None))
        if dept_id is not None:        # 부서 경계(§6.2) — MANAGER 본부서 직원만
            q = q.where(Employee.department_id == dept_id)
        if employee_id is not None:    # SELF — 본인만
            q = q.where(LeaveAbsence.employee_id == employee_id)
        return [{"id": a.id, "employee_id": a.employee_id, "employee_no": no,
                 "name": name, "kind": a.kind, "start_on": a.start_on,
                 "expected_return_on": a.expected_return_on, "return_on": a.return_on,
                 "insurance_exempt": a.insurance_exempt, "note": a.note}
                for a, no, name in (await session.execute(q)).all()]

    async def resign(self, session: AsyncSession, *, actor: int, employee_id: int,
                     resigned_on: date, note: str | None = None,
                     reason: str | None = None) -> Employee:
        """퇴사 처리 — resigned_on 확정 + 발령(RESIGN). HIS 관리 직원은 차단(SoT)."""
        emp = await session.get(Employee, employee_id)
        if emp is None:
            raise ValidationError("존재하지 않는 직원입니다")
        if emp.source == "HIS":
            raise ForbiddenError("HIS 관리 직원의 퇴사는 HIS에서 처리 — 동기화로 반영됩니다")
        if emp.resigned_on is not None:
            raise ValidationError("이미 퇴사 처리된 직원입니다")
        if resigned_on < emp.hired_on:
            raise ValidationError("퇴사일은 입사일 이후여야 합니다")
        emp.resigned_on = resigned_on
        emp.resign_reason = reason
        session.add(Appointment(employee_id=employee_id, appointment_type="RESIGN",
                                from_department_id=emp.department_id,
                                from_position=emp.position,
                                effective_on=resigned_on, note=note))
        await _ins.record(session, employee_id=employee_id, event="LOSE",
                          basis_on=resigned_on)      # 상실 신고 추적 (P1-③)
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="hr.employees", target_id=str(employee_id),
                                  after={"resigned_on": str(resigned_on)})
        return emp
