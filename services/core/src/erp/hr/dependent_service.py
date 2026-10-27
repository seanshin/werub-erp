"""부양가족 서비스 (P3) — 등록(공제대상 자동 제안)·삭제·공제 인원 산출.

자동 제안 규칙(연령 기준 — 소득요건 등은 담당자 판단·counted 수정):
배우자·장애인=산입, 자녀(CHILD) 만 20세 이하, 직계존속(PARENT·GRANDPARENT) 만 60세 이상.
급여 부양가족 수 = 본인(1) + counted 가족 수 — generate 폴백 체인에 사용.
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.hr.models import Dependent, Employee

_audit = AuditService()


def _age(birth: date | None, on: date) -> int | None:
    if birth is None:
        return None
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def suggest_counted(relation: str, birth_date: date | None, disabled: bool,
                    on: date | None = None) -> bool:
    """공제대상 자동 제안 — 연령 기준(소득요건은 담당자 확정)."""
    if disabled or relation == "SPOUSE":
        return True
    age = _age(birth_date, on or date.today())
    if age is None:
        return False
    if relation == "CHILD":
        return age <= 20
    if relation in ("PARENT", "GRANDPARENT"):
        return age >= 60
    return False


class DependentService:
    async def add(self, session: AsyncSession, *, actor: int, employee_id: int,
                  name: str, relation: str, birth_date: date | None = None,
                  disabled: bool = False, counted: bool | None = None,
                  note: str | None = None) -> Dependent:
        if await session.get(Employee, employee_id) is None:
            raise ValidationError("존재하지 않는 직원입니다")
        d = Dependent(employee_id=employee_id, name=name, relation=relation,
                      birth_date=birth_date, disabled=disabled,
                      counted=counted if counted is not None
                      else suggest_counted(relation, birth_date, disabled),
                      note=note, created_by=actor)
        session.add(d)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="hr.dependents", target_id=str(d.id),
                                  after={"employee_id": employee_id, "relation": relation,
                                         "counted": d.counted})
        return d

    async def remove(self, session: AsyncSession, *, actor: int, dependent_id: int) -> None:
        d = await session.get(Dependent, dependent_id)
        if d is None:
            raise ValidationError("존재하지 않는 부양가족입니다")
        await session.delete(d)
        await _audit.record_audit(session, actor=actor, action="DELETE",
                                  entity="hr.dependents", target_id=str(dependent_id),
                                  before={"employee_id": d.employee_id, "name": d.name})

    async def list(self, session: AsyncSession, *, employee_id: int) -> list[dict]:
        rows = (await session.scalars(
            select(Dependent).where(Dependent.employee_id == employee_id)
            .order_by(Dependent.id))).all()
        return [{"id": d.id, "name": d.name, "relation": d.relation,
                 "birth_date": d.birth_date, "disabled": d.disabled,
                 "counted": d.counted, "note": d.note} for d in rows]

    async def payroll_dependents(self, session: AsyncSession,
                                 employee_id: int) -> int | None:
        """급여용 부양가족 수 = 본인 1 + counted 가족. 등록 0건이면 None(프로필 폴백)."""
        total = await session.scalar(
            select(func.count(Dependent.id)).where(Dependent.employee_id == employee_id))
        if not total:
            return None
        counted = await session.scalar(
            select(func.count(Dependent.id)).where(Dependent.employee_id == employee_id,
                                                   Dependent.counted.is_(True)))
        return 1 + int(counted or 0)
