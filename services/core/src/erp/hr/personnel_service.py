"""직급·호봉 체계 + 징계·상벌 (Tier3·P3-8) — 연도별 호봉표 마스터·인사 상벌 기록.

호봉표: (직급, 호봉, 연도)별 기본급 upsert·조회·lookup(급여 산정 참조). 상벌: 징계/포상 등록·이력.
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.hr.models import DisciplinaryAction, Employee, PayGradeStep


class PayGradeService:
    async def upsert_step(self, session: AsyncSession, *, grade: str, step: int, base_pay: float,
                          effective_year: int, note: str | None = None) -> PayGradeStep:
        if not (grade or "").strip() or step <= 0 or base_pay <= 0:
            raise ValidationError("직급·호봉·기본급을 확인하세요")
        row = await session.scalar(select(PayGradeStep).where(
            PayGradeStep.grade == grade, PayGradeStep.step == step,
            PayGradeStep.effective_year == effective_year))
        if row is None:
            row = PayGradeStep(grade=grade, step=step, effective_year=effective_year)
            session.add(row)
        row.base_pay, row.note = base_pay, note
        await session.flush()
        return row

    async def list_steps(self, session: AsyncSession, *, effective_year: int) -> list[dict]:
        rows = (await session.scalars(select(PayGradeStep).where(
            PayGradeStep.effective_year == effective_year)
            .order_by(PayGradeStep.grade, PayGradeStep.step))).all()
        return [{"id": r.id, "grade": r.grade, "step": r.step, "base_pay": float(r.base_pay),
                 "effective_year": r.effective_year, "note": r.note} for r in rows]

    async def lookup(self, session: AsyncSession, *, grade: str, step: int,
                     year: int) -> dict | None:
        r = await session.scalar(select(PayGradeStep).where(
            PayGradeStep.grade == grade, PayGradeStep.step == step,
            PayGradeStep.effective_year == year))
        return {"grade": r.grade, "step": r.step, "base_pay": float(r.base_pay),
                "effective_year": r.effective_year} if r else None


class DisciplinaryService:
    async def record(self, session: AsyncSession, *, employee_id: int, action_type: str,
                     category: str, reason: str, decided_on: date,
                     effective_from: date | None = None, effective_to: date | None = None,
                     note: str | None = None, actor: int | None = None) -> DisciplinaryAction:
        emp = await session.get(Employee, employee_id)
        if emp is None:
            raise ValidationError("존재하지 않는 직원입니다")
        if action_type not in ("DISCIPLINE", "REWARD"):
            raise ValidationError("action_type은 DISCIPLINE|REWARD")
        if not (category or "").strip() or not (reason or "").strip():
            raise ValidationError("종류·사유는 필수입니다")
        if effective_to is not None and effective_from is not None and effective_to < effective_from:
            raise ValidationError("효력 종료일은 시작일 이후여야 합니다")
        a = DisciplinaryAction(employee_id=employee_id, action_type=action_type,
                               category=category, reason=reason, decided_on=decided_on,
                               effective_from=effective_from, effective_to=effective_to,
                               note=note, created_by=actor)
        session.add(a)
        await session.flush()
        return a

    async def list_by_employee(self, session: AsyncSession, *, employee_id: int) -> list[dict]:
        rows = (await session.scalars(select(DisciplinaryAction).where(
            DisciplinaryAction.employee_id == employee_id)
            .order_by(DisciplinaryAction.decided_on.desc()))).all()
        return [self._dto(r) for r in rows]

    async def recent(self, session: AsyncSession, *, action_type: str | None = None,
                     limit: int = 100) -> list[dict]:
        """전사 상벌 최근 목록 — 대상자를 원시 PK(#12)로만 찍으면 특정할 수 없어 이름을 병기한다.

        행 스코프는 라우터(hr.payroll)가 이미 강제하므로 이름 병기로 노출이 넓어지지 않는다."""
        from erp.hr.employee_service import employee_labels
        q = select(DisciplinaryAction).order_by(DisciplinaryAction.decided_on.desc())
        if action_type:
            q = q.where(DisciplinaryAction.action_type == action_type)
        rows = (await session.scalars(q.limit(min(limit, 500)))).all()
        labels = await employee_labels(session, [r.employee_id for r in rows])
        return [self._dto(r) | {k: v for k, v in
                                (labels.get(r.employee_id) or {}).items()} for r in rows]

    def _dto(self, r: DisciplinaryAction) -> dict:
        return {"id": r.id, "employee_id": r.employee_id, "action_type": r.action_type,
                "category": r.category, "reason": r.reason, "decided_on": str(r.decided_on),
                "effective_from": str(r.effective_from) if r.effective_from else None,
                "effective_to": str(r.effective_to) if r.effective_to else None, "note": r.note}
