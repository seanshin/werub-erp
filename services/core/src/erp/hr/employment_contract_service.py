"""일반직 근로계약 (근기법 §17·P3-5) — 근로조건 명시·이력. 직원당 ACTIVE 1건(신규 발효 시 승계).

의료진(MEDICAL)은 MedicalStaffContract. 본 서비스는 일반직(SUPPORT/비MEDICAL) 대상.
생애주기: DRAFT(작성)→ACTIVE(발효·기존 종료)→ENDED(종료). 계약직 갱신(valid_to 연장).
"""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.hr.models import Employee, EmploymentContract

_audit = AuditService()
_TYPES = {"REGULAR", "FIXED_TERM", "PART_TIME"}
_ENT = "hr.employment_contracts"


class EmploymentContractService:
    async def create(self, session: AsyncSession, *, actor: int, employee_id: int,
                     contract_type: str, valid_from: date, base_pay: float,
                     valid_to: date | None = None, weekly_hours: float | None = None,
                     work_schedule: str | None = None, work_location: str | None = None,
                     job_duties: str | None = None, note: str | None = None,
                     activate: bool = False) -> EmploymentContract:
        emp = await session.get(Employee, employee_id)
        if emp is None:
            raise ValidationError("존재하지 않는 직원입니다")
        if emp.staff_class == "MEDICAL":
            raise ValidationError("의료진은 의료계약(MedicalStaffContract) 대상입니다")
        if contract_type not in _TYPES:
            raise ValidationError(f"계약유형 오류: {contract_type}")
        if base_pay <= 0:
            raise ValidationError("기본급은 0보다 커야 합니다")
        if contract_type == "FIXED_TERM" and valid_to is None:
            raise ValidationError("계약직(FIXED_TERM)은 만료일(valid_to)이 필요합니다")
        if valid_to is not None and valid_to <= valid_from:
            raise ValidationError("만료일은 시작일 이후여야 합니다")
        if activate:
            await self._supersede_active(session, employee_id, valid_from)
        c = EmploymentContract(
            employee_id=employee_id, contract_type=contract_type, valid_from=valid_from,
            valid_to=valid_to, base_pay=base_pay, weekly_hours=weekly_hours,
            work_schedule=work_schedule, work_location=work_location, job_duties=job_duties,
            note=note, status="ACTIVE" if activate else "DRAFT", created_by=actor)
        session.add(c)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE", entity=_ENT,
                                  target_id=str(c.id),
                                  after={"employee_id": employee_id, "type": contract_type,
                                         "base_pay": base_pay, "status": c.status})
        return c

    async def _supersede_active(self, session: AsyncSession, employee_id: int, from_on: date,
                                *, exclude_id: int | None = None) -> None:
        prev = await session.scalar(select(EmploymentContract).where(
            EmploymentContract.employee_id == employee_id,
            EmploymentContract.status == "ACTIVE"))
        if prev is None or (exclude_id is not None and prev.id == exclude_id):
            return
        if from_on <= prev.valid_from:
            raise ValidationError("신규 계약 시작일은 기존 계약 시작일 이후여야 합니다")
        prev.valid_to = from_on - timedelta(days=1)
        prev.status, prev.ended_on = "ENDED", from_on - timedelta(days=1)

    async def activate(self, session: AsyncSession, *, actor: int,
                       contract_id: int) -> EmploymentContract:
        c = await self._get(session, contract_id)
        if c.status != "DRAFT":
            raise ValidationError(f"발효 가능한 상태가 아닙니다 ({c.status})")
        await self._supersede_active(session, c.employee_id, c.valid_from, exclude_id=c.id)
        c.status = "ACTIVE"
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity=_ENT,
                                  target_id=str(c.id), after={"status": "ACTIVE"})
        return c

    async def end(self, session: AsyncSession, *, actor: int, contract_id: int,
                  end_on: date) -> EmploymentContract:
        c = await self._get(session, contract_id)
        if c.status not in ("ACTIVE", "DRAFT"):
            raise ValidationError(f"종료 가능한 상태가 아닙니다 ({c.status})")
        c.status, c.ended_on = "ENDED", end_on
        if c.valid_to is None or end_on < c.valid_to:
            c.valid_to = end_on
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity=_ENT,
                                  target_id=str(c.id), after={"status": "ENDED", "ended_on": str(end_on)})
        return c

    async def renew(self, session: AsyncSession, *, actor: int, contract_id: int,
                    new_valid_to: date) -> EmploymentContract:
        c = await self._get(session, contract_id)
        if c.status != "ACTIVE":
            raise ValidationError("활성 계약만 갱신할 수 있습니다")
        if c.valid_to is not None and new_valid_to <= c.valid_to:
            raise ValidationError("갱신 만료일은 기존 만료일 이후여야 합니다")
        c.valid_to = new_valid_to
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity=_ENT,
                                  target_id=str(c.id), after={"valid_to": str(new_valid_to)})
        return c

    async def list_by_employee(self, session: AsyncSession, *, employee_id: int) -> list[dict]:
        rows = (await session.scalars(select(EmploymentContract).where(
            EmploymentContract.employee_id == employee_id)
            .order_by(EmploymentContract.valid_from.desc()))).all()
        return [self._dto(c) for c in rows]

    async def list_expiring(self, session: AsyncSession, *, within_days: int = 30,
                            as_of: date | None = None) -> list[dict]:
        """만기 임박 계약직(FIXED_TERM ACTIVE·valid_to 근접) — 갱신/종료 판단."""
        as_of = as_of or date.today()
        cutoff = as_of + timedelta(days=within_days)
        rows = (await session.scalars(select(EmploymentContract).where(
            EmploymentContract.status == "ACTIVE",
            EmploymentContract.valid_to.isnot(None),
            EmploymentContract.valid_to <= cutoff).order_by(EmploymentContract.valid_to))).all()
        # 만기표가 `#12`로만 표시돼 60일 내 만기자를 이름 없이 판단할 수 없었다(카드가 사실상 무용)
        from erp.hr.employee_service import employee_labels
        labels = await employee_labels(session, [c.employee_id for c in rows])
        return [{**self._dto(c), "d_day": (c.valid_to - as_of).days,
                 "employee_name": labels.get(c.employee_id, {}).get("name"),
                 "employee_no": labels.get(c.employee_id, {}).get("employee_no")} for c in rows]

    def _dto(self, c: EmploymentContract) -> dict:
        return {"id": c.id, "employee_id": c.employee_id, "contract_type": c.contract_type,
                "valid_from": str(c.valid_from), "valid_to": str(c.valid_to) if c.valid_to else None,
                "status": c.status, "base_pay": float(c.base_pay),
                "weekly_hours": float(c.weekly_hours) if c.weekly_hours is not None else None,
                "work_schedule": c.work_schedule, "work_location": c.work_location,
                "job_duties": c.job_duties, "note": c.note,
                "ended_on": str(c.ended_on) if c.ended_on else None}

    async def _get(self, session: AsyncSession, contract_id: int) -> EmploymentContract:
        c = await session.get(EmploymentContract, contract_id)
        if c is None:
            raise ValidationError("존재하지 않는 근로계약입니다")
        return c
