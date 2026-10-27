"""인사평가 (Tier3·0229) — 차수·항목·평가 배정·자기평가·평가자 채점·확정(가중 총점·등급).

흐름: 차수 생성(DRAFT)+항목 편성 → OPEN+대상 배정 → 자기평가(SELF_DONE) → 평가자 채점(EVAL_DONE)
→ 확정(CONFIRMED·가중 총점→등급). 가중 총점 = Σ(점수/만점 × 가중치) / Σ가중치 × 100.
"""
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ForbiddenError, ValidationError
from erp.hr.models import (
    Employee, EvalCriterion, EvalCycle, EvalScore, Evaluation,
)

_audit = AuditService()
_DEFAULT_BANDS = [{"grade": "S", "min": 90}, {"grade": "A", "min": 80},
                  {"grade": "B", "min": 70}, {"grade": "C", "min": 60}, {"grade": "D", "min": 0}]


def _grade_for(total: float, bands: list | None) -> str:
    for b in sorted(bands or _DEFAULT_BANDS, key=lambda x: -x["min"]):
        if total >= b["min"]:
            return b["grade"]
    return "D"


class EvaluationService:
    # ── 차수·항목 ───────────────────────────────────────────
    async def create_cycle(self, session: AsyncSession, *, actor: int, period: str, title: str,
                           cycle_type: str = "ANNUAL", start_on: date | None = None,
                           end_on: date | None = None,
                           grade_bands: list | None = None) -> EvalCycle:
        if not period.strip() or not title.strip():
            raise ValidationError("평가 기간·명칭은 필수입니다")
        if await session.scalar(select(EvalCycle.id).where(EvalCycle.period == period)):
            raise ValidationError(f"이미 존재하는 평가 기간: {period}")
        c = EvalCycle(period=period.strip()[:16], title=title.strip()[:160], cycle_type=cycle_type,
                      start_on=start_on, end_on=end_on, grade_bands=grade_bands, created_by=actor)
        session.add(c)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE", entity="hr.eval_cycles",
                                  target_id=str(c.id), after={"period": period})
        return c

    async def add_criterion(self, session: AsyncSession, *, actor: int, cycle_id: int,
                            title: str, kind: str = "COMPETENCY", weight: float = 1,
                            max_score: float = 100, sort_order: int = 0) -> EvalCriterion:
        cyc = await session.get(EvalCycle, cycle_id)
        if cyc is None:
            raise ValidationError("존재하지 않는 평가 차수입니다")
        if cyc.status != "DRAFT":
            raise ValidationError("항목은 DRAFT 상태에서만 편성할 수 있습니다", {"status": cyc.status})
        if weight <= 0 or max_score <= 0:
            raise ValidationError("가중치·만점은 0보다 커야 합니다")
        cr = EvalCriterion(cycle_id=cycle_id, kind=kind, title=title[:160], weight=weight,
                           max_score=max_score, sort_order=sort_order)
        session.add(cr)
        await session.flush()
        return cr

    async def criteria(self, session: AsyncSession, cycle_id: int) -> list[EvalCriterion]:
        return list((await session.scalars(select(EvalCriterion).where(
            EvalCriterion.cycle_id == cycle_id)
            .order_by(EvalCriterion.sort_order, EvalCriterion.id))).all())

    async def open_cycle(self, session: AsyncSession, *, actor: int, cycle_id: int) -> EvalCycle:
        cyc = await session.get(EvalCycle, cycle_id, with_for_update=True)
        if cyc is None:
            raise ValidationError("존재하지 않는 평가 차수입니다")
        if cyc.status != "DRAFT":
            raise ValidationError("DRAFT 차수만 개시할 수 있습니다", {"status": cyc.status})
        if not await self.criteria(session, cycle_id):
            raise ValidationError("평가 항목을 1개 이상 편성해야 개시할 수 있습니다")
        cyc.status = "OPEN"
        return cyc

    async def close_cycle(self, session: AsyncSession, *, actor: int, cycle_id: int) -> EvalCycle:
        cyc = await session.get(EvalCycle, cycle_id, with_for_update=True)
        if cyc is None or cyc.status != "OPEN":
            raise ValidationError("OPEN 차수만 마감할 수 있습니다")
        cyc.status = "CLOSED"
        return cyc

    async def list_cycles(self, session: AsyncSession) -> list[dict]:
        rows = (await session.scalars(select(EvalCycle).order_by(EvalCycle.id.desc()))).all()
        counts = dict((await session.execute(
            select(Evaluation.cycle_id, func.count()).group_by(Evaluation.cycle_id))).all())
        return [{"id": c.id, "period": c.period, "title": c.title, "cycle_type": c.cycle_type,
                 "status": c.status, "start_on": c.start_on, "end_on": c.end_on,
                 "evaluations": counts.get(c.id, 0)} for c in rows]

    # ── 배정 ────────────────────────────────────────────────
    async def assign(self, session: AsyncSession, *, actor: int, cycle_id: int,
                     employee_id: int, evaluator_id: int | None = None) -> Evaluation:
        cyc = await session.get(EvalCycle, cycle_id)
        if cyc is None or cyc.status != "OPEN":
            raise ValidationError("OPEN 차수에만 배정할 수 있습니다")
        if await session.get(Employee, employee_id) is None:
            raise ValidationError("존재하지 않는 직원입니다")
        existing = await session.scalar(select(Evaluation).where(
            Evaluation.cycle_id == cycle_id, Evaluation.employee_id == employee_id))
        if existing is not None:
            if evaluator_id is not None:
                existing.evaluator_id = evaluator_id
            return existing
        ev = Evaluation(cycle_id=cycle_id, employee_id=employee_id, evaluator_id=evaluator_id,
                        status="PENDING")
        session.add(ev)
        await session.flush()
        return ev

    async def assign_bulk(self, session: AsyncSession, *, actor: int, cycle_id: int,
                          assignments: list[dict]) -> dict:
        """[{employee_id, evaluator_id?}] 일괄 배정(멱등)."""
        added = 0
        for a in assignments:
            before = await session.scalar(select(Evaluation.id).where(
                Evaluation.cycle_id == cycle_id, Evaluation.employee_id == a["employee_id"]))
            await self.assign(session, actor=actor, cycle_id=cycle_id,
                              employee_id=a["employee_id"], evaluator_id=a.get("evaluator_id"))
            if before is None:
                added += 1
        return {"cycle_id": cycle_id, "requested": len(assignments), "added": added}

    async def _get_eval(self, session: AsyncSession, eval_id: int, *, lock: bool = False):
        ev = await session.get(Evaluation, eval_id, with_for_update=lock)
        if ev is None:
            raise ValidationError("존재하지 않는 평가입니다")
        return ev

    def _weighted_total(self, criteria: list[EvalCriterion], scores: dict, field: str) -> float:
        """Σ(점수/만점 × 가중치) / Σ가중치 × 100. 점수 없으면 0으로 산입."""
        wsum = sum(float(c.weight) for c in criteria)
        if wsum <= 0:
            return 0.0
        acc = 0.0
        for c in criteria:
            s = scores.get(c.id)
            v = getattr(s, field, None) if s is not None else None
            if v is not None and float(c.max_score) > 0:
                acc += (float(v) / float(c.max_score)) * float(c.weight)
        return round(acc / wsum * 100, 2)

    # ── 자기평가 ────────────────────────────────────────────
    async def submit_self(self, session: AsyncSession, *, eval_id: int, self_emp: int | None,
                          scores: list[dict], comment: str | None = None) -> Evaluation:
        """자기평가 제출 — 본인만(self_emp==대상). scores=[{criterion_id, score}]."""
        ev = await self._get_eval(session, eval_id, lock=True)
        if self_emp is None or ev.employee_id != self_emp:
            raise ForbiddenError("본인 평가만 자기평가할 수 있습니다")
        if ev.status not in ("PENDING", "SELF_DONE"):
            raise ValidationError("자기평가 가능한 상태가 아닙니다", {"status": ev.status})
        crits = {c.id: c for c in await self.criteria(session, ev.cycle_id)}
        await self._upsert_scores(session, ev, scores, crits, field="self_score")
        row = {s.criterion_id: s for s in await self._scores(session, eval_id)}
        ev.self_total = self._weighted_total(list(crits.values()), row, "self_score")
        ev.self_comment = comment
        if ev.status == "PENDING":
            ev.status = "SELF_DONE"
        return ev

    # ── 평가자 채점 ─────────────────────────────────────────
    async def submit_eval(self, session: AsyncSession, *, eval_id: int, actor: int, actor_scope,
                          scores: list[dict], comment: str | None = None) -> Evaluation:
        """평가자 채점 — 지정 평가자 본인 또는 ALL/DEPT 권한자. scores=[{criterion_id, score}]."""
        ev = await self._get_eval(session, eval_id, lock=True)
        await self._assert_evaluator(session, ev, actor, actor_scope)
        if ev.status not in ("SELF_DONE", "PENDING", "EVAL_DONE"):
            raise ValidationError("채점 가능한 상태가 아닙니다", {"status": ev.status})
        crits = {c.id: c for c in await self.criteria(session, ev.cycle_id)}
        await self._upsert_scores(session, ev, scores, crits, field="eval_score")
        row = {s.criterion_id: s for s in await self._scores(session, eval_id)}
        ev.eval_total = self._weighted_total(list(crits.values()), row, "eval_score")
        ev.evaluator_comment = comment
        ev.status = "EVAL_DONE"
        return ev

    async def confirm(self, session: AsyncSession, *, eval_id: int, actor: int,
                      final_grade: str | None = None) -> Evaluation:
        """확정 — EVAL_DONE만. 등급 미지정 시 eval_total로 자동 산정(차수 밴드)."""
        ev = await self._get_eval(session, eval_id, lock=True)
        if ev.status != "EVAL_DONE":
            raise ValidationError("평가자 채점 완료 후에만 확정할 수 있습니다", {"status": ev.status})
        cyc = await session.get(EvalCycle, ev.cycle_id)
        ev.final_grade = final_grade or _grade_for(float(ev.eval_total or 0),
                                                   cyc.grade_bands if cyc else None)
        ev.status, ev.confirmed_by = "CONFIRMED", actor
        ev.confirmed_at = datetime.now(timezone.utc)
        await _audit.record_audit(session, actor=actor, action="CONFIRM", entity="hr.evaluations",
                                  target_id=str(ev.id),
                                  after={"grade": ev.final_grade, "total": float(ev.eval_total or 0)})
        return ev

    # ── 내부 ────────────────────────────────────────────────
    async def _assert_evaluator(self, session, ev: Evaluation, actor: int, scope) -> None:
        from erp.common.policy import ScopeKind
        if scope.kind == ScopeKind.ALL:
            return
        if ev.evaluator_id == actor:
            return
        if scope.kind == ScopeKind.DEPT:
            emp = await session.get(Employee, ev.employee_id)
            if emp is not None and emp.department_id == scope.dept_id:
                return
        raise ForbiddenError("지정 평가자 또는 부서 관리자만 채점할 수 있습니다")

    async def _scores(self, session, eval_id: int) -> list[EvalScore]:
        return list((await session.scalars(select(EvalScore).where(
            EvalScore.evaluation_id == eval_id))).all())

    async def _upsert_scores(self, session, ev: Evaluation, scores: list[dict],
                             crits: dict, *, field: str) -> None:
        existing = {s.criterion_id: s for s in await self._scores(session, ev.id)}
        for item in scores:
            cid = item.get("criterion_id")
            if cid not in crits:
                raise ValidationError("평가 항목에 없는 점수입니다", {"criterion_id": cid})
            val = item.get("score")
            if val is not None and (float(val) < 0 or float(val) > float(crits[cid].max_score)):
                raise ValidationError(
                    f"점수는 0~{float(crits[cid].max_score)} 범위여야 합니다", {"criterion_id": cid})
            row = existing.get(cid)
            if row is None:
                row = EvalScore(evaluation_id=ev.id, criterion_id=cid)
                session.add(row)
            setattr(row, field, val)
            if item.get("note") is not None:
                row.note = str(item["note"])[:300]
        await session.flush()

    # ── 조회 ────────────────────────────────────────────────
    async def get_detail(self, session: AsyncSession, eval_id: int) -> dict:
        ev = await self._get_eval(session, eval_id)
        crits = await self.criteria(session, ev.cycle_id)
        scores = {s.criterion_id: s for s in await self._scores(session, eval_id)}
        emp = await session.get(Employee, ev.employee_id)
        return {"id": ev.id, "cycle_id": ev.cycle_id, "employee_id": ev.employee_id,
                "employee_name": emp.name if emp else None,
                "evaluator_id": ev.evaluator_id, "status": ev.status,
                "self_total": float(ev.self_total) if ev.self_total is not None else None,
                "eval_total": float(ev.eval_total) if ev.eval_total is not None else None,
                "final_grade": ev.final_grade, "self_comment": ev.self_comment,
                "evaluator_comment": ev.evaluator_comment,
                "criteria": [{"id": c.id, "kind": c.kind, "title": c.title,
                              "weight": float(c.weight), "max_score": float(c.max_score),
                              "self_score": float(scores[c.id].self_score)
                              if c.id in scores and scores[c.id].self_score is not None else None,
                              "eval_score": float(scores[c.id].eval_score)
                              if c.id in scores and scores[c.id].eval_score is not None else None}
                             for c in crits]}

    async def list_evaluations(self, session: AsyncSession, *, cycle_id: int,
                               dept_id: int | None = None,
                               employee_id: int | None = None) -> list[dict]:
        q = (select(Evaluation, Employee.name, Employee.employee_no, Employee.department_id)
             .join(Employee, Employee.id == Evaluation.employee_id)
             .where(Evaluation.cycle_id == cycle_id))
        if dept_id is not None:
            q = q.where(Employee.department_id == dept_id)
        if employee_id is not None:
            q = q.where(Evaluation.employee_id == employee_id)
        rows = (await session.execute(q.order_by(Employee.employee_no))).all()
        return [{"id": ev.id, "employee_id": ev.employee_id, "employee_no": no, "name": name,
                 "department_id": dept, "evaluator_id": ev.evaluator_id, "status": ev.status,
                 "self_total": float(ev.self_total) if ev.self_total is not None else None,
                 "eval_total": float(ev.eval_total) if ev.eval_total is not None else None,
                 "final_grade": ev.final_grade} for ev, name, no, dept in rows]
