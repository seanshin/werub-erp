"""원내 교육이수 (Tier3·0228) — 과정 마스터·회차 편성·이수/수료·미이수 현황.

법정의무교육(compliance_service·6종 고정)과 별개로 임의 사내 과정을 데이터 기반으로 관리.
필수(mandatory) 과정은 대상 재직자 × 최신 이수로 상태(OK/DUE_SOON/OVERDUE/NONE) 매트릭스 산출.
"""
from calendar import monthrange
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.hr.models import (
    Employee, TrainingCompletion, TrainingCourse, TrainingSession,
)

_audit = AuditService()
DUE_SOON_DAYS = 30


def _add_months(d: date, months: int) -> date:
    """월 가산 — 말일 클램프(1/31 + 1개월 = 2/28|29). dateutil 무의존."""
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def _applies(course: TrainingCourse, emp: Employee) -> bool:
    """과정 대상 판정 — ALL·직종·부서."""
    if course.target == "JOB_CATEGORY":
        return (emp.job_category or "") == (course.target_value or "")
    if course.target == "DEPT":
        return str(emp.department_id or "") == (course.target_value or "")
    return True


class TrainingService:
    # ── 과정 마스터 ─────────────────────────────────────────
    async def create_course(self, session: AsyncSession, *, actor: int, code: str, title: str,
                            category: str = "JOB", cycle_months: int = 0, target: str = "ALL",
                            target_value: str | None = None, mandatory: bool = True,
                            note: str | None = None) -> TrainingCourse:
        if not code.strip() or not title.strip():
            raise ValidationError("과정 코드·명칭은 필수입니다")
        if cycle_months < 0:
            raise ValidationError("주기(개월)는 0 이상이어야 합니다")
        if target in ("JOB_CATEGORY", "DEPT") and not (target_value or "").strip():
            raise ValidationError("대상 한정(직종/부서) 시 대상값이 필요합니다")
        if await session.scalar(select(TrainingCourse.id).where(TrainingCourse.code == code)):
            raise ValidationError(f"이미 존재하는 과정 코드: {code}")
        c = TrainingCourse(code=code.strip()[:30], title=title.strip()[:160], category=category,
                           cycle_months=cycle_months, target=target,
                           target_value=(target_value or None), mandatory=mandatory,
                           note=note, created_by=actor)
        session.add(c)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="hr.training_courses", target_id=str(c.id),
                                  after={"code": code, "title": title})
        return c

    async def list_courses(self, session: AsyncSession, *, active: bool | None = None) -> list[dict]:
        q = select(TrainingCourse)
        if active is not None:
            q = q.where(TrainingCourse.active.is_(active))
        rows = (await session.scalars(q.order_by(TrainingCourse.code))).all()
        counts = dict((await session.execute(
            select(TrainingSession.course_id, func.count())
            .group_by(TrainingSession.course_id))).all())
        return [{"id": c.id, "code": c.code, "title": c.title, "category": c.category,
                 "cycle_months": c.cycle_months, "target": c.target,
                 "target_value": c.target_value, "mandatory": c.mandatory, "active": c.active,
                 "note": c.note, "sessions": counts.get(c.id, 0)} for c in rows]

    async def set_course_active(self, session: AsyncSession, *, actor: int, course_id: int,
                                active: bool) -> TrainingCourse:
        c = await session.get(TrainingCourse, course_id)
        if c is None:
            raise ValidationError("존재하지 않는 과정입니다")
        c.active = active
        return c

    # ── 회차 ────────────────────────────────────────────────
    async def create_session(self, session: AsyncSession, *, actor: int, course_id: int,
                             held_on: date, hours: float = 1, method: str = "OFFLINE",
                             instructor: str | None = None, location: str | None = None,
                             note: str | None = None) -> TrainingSession:
        c = await session.get(TrainingCourse, course_id)
        if c is None:
            raise ValidationError("존재하지 않는 과정입니다")
        s = TrainingSession(course_id=course_id, held_on=held_on, hours=hours, method=method,
                            instructor=instructor, location=location, note=note, created_by=actor)
        session.add(s)
        await session.flush()
        return s

    async def list_sessions(self, session: AsyncSession, *, course_id: int) -> list[dict]:
        rows = (await session.scalars(select(TrainingSession).where(
            TrainingSession.course_id == course_id).order_by(TrainingSession.held_on.desc()))).all()
        done = dict((await session.execute(
            select(TrainingCompletion.session_id, func.count())
            .where(TrainingCompletion.session_id.in_([s.id for s in rows]) if rows else False)
            .group_by(TrainingCompletion.session_id))).all()) if rows else {}
        return [{"id": s.id, "held_on": s.held_on, "hours": float(s.hours), "method": s.method,
                 "instructor": s.instructor, "location": s.location, "note": s.note,
                 "completions": done.get(s.id, 0)} for s in rows]

    # ── 이수 ────────────────────────────────────────────────
    async def _valid_until(self, session: AsyncSession, course: TrainingCourse,
                           completed_on: date) -> date | None:
        if course.cycle_months <= 0:
            return None
        return _add_months(completed_on, course.cycle_months)

    async def record_completion(self, session: AsyncSession, *, actor: int, session_id: int,
                                employee_id: int, completed_on: date | None = None,
                                score: float | None = None, cert_no: str | None = None,
                                note: str | None = None) -> TrainingCompletion:
        ses = await session.get(TrainingSession, session_id)
        if ses is None:
            raise ValidationError("존재하지 않는 회차입니다")
        if await session.get(Employee, employee_id) is None:
            raise ValidationError("존재하지 않는 직원입니다")
        existing = await session.scalar(select(TrainingCompletion).where(
            TrainingCompletion.session_id == session_id,
            TrainingCompletion.employee_id == employee_id))
        if existing is not None:
            return existing                              # (회차,직원) 멱등
        course = await session.get(TrainingCourse, ses.course_id)
        done = completed_on or ses.held_on
        rec = TrainingCompletion(
            session_id=session_id, course_id=ses.course_id, employee_id=employee_id,
            completed_on=done, valid_until=await self._valid_until(session, course, done),
            score=score, cert_no=cert_no, note=note, created_by=actor)
        session.add(rec)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="hr.training_completions", target_id=str(rec.id),
                                  after={"session_id": session_id, "employee_id": employee_id})
        return rec

    async def record_bulk(self, session: AsyncSession, *, actor: int, session_id: int,
                          employee_ids: list[int]) -> dict:
        """회차 이수 일괄 등록 — 참석자 명단 처리(멱등)."""
        added = 0
        for eid in employee_ids:
            before = await session.scalar(select(TrainingCompletion.id).where(
                TrainingCompletion.session_id == session_id,
                TrainingCompletion.employee_id == eid))
            await self.record_completion(session, actor=actor, session_id=session_id,
                                         employee_id=eid)
            if before is None:
                added += 1
        return {"session_id": session_id, "requested": len(employee_ids), "added": added}

    async def completions(self, session: AsyncSession, *, employee_id: int) -> list[dict]:
        rows = (await session.execute(
            select(TrainingCompletion, TrainingCourse.code, TrainingCourse.title,
                   TrainingSession.held_on)
            .join(TrainingCourse, TrainingCourse.id == TrainingCompletion.course_id)
            .join(TrainingSession, TrainingSession.id == TrainingCompletion.session_id)
            .where(TrainingCompletion.employee_id == employee_id)
            .order_by(TrainingCompletion.completed_on.desc()))).all()
        return [{"id": r.id, "course_code": code, "course_title": title, "held_on": held,
                 "completed_on": r.completed_on, "valid_until": r.valid_until,
                 "score": float(r.score) if r.score is not None else None,
                 "cert_no": r.cert_no} for r, code, title, held in rows]

    # ── 미이수 현황 (필수 과정 × 대상 재직자) ────────────────
    async def status(self, session: AsyncSession, *, as_of: date | None = None) -> dict:
        today = as_of or date.today()
        courses = (await session.scalars(select(TrainingCourse).where(
            TrainingCourse.active.is_(True), TrainingCourse.mandatory.is_(True))
            .order_by(TrainingCourse.code))).all()
        employees = (await session.scalars(select(Employee).where(
            Employee.resigned_on.is_(None)).order_by(Employee.employee_no))).all()
        # (직원,과정) 최신 이수 — completed_on 오름차순 → 마지막이 최신
        latest: dict[tuple[int, int], TrainingCompletion] = {}
        for r in (await session.scalars(select(TrainingCompletion)
                                        .order_by(TrainingCompletion.completed_on))).all():
            latest[(r.employee_id, r.course_id)] = r
        items = []
        summary = {c.id: {"required": 0, "ok": 0} for c in courses}
        for emp in employees:
            cells = {}
            for c in courses:
                if not _applies(c, emp):
                    cells[c.code] = {"state": "N/A"}
                    continue
                summary[c.id]["required"] += 1
                rec = latest.get((emp.id, c.id))
                if rec is None:
                    cells[c.code] = {"state": "NONE"}
                elif rec.valid_until is not None and rec.valid_until < today:
                    cells[c.code] = {"state": "OVERDUE", "completed_on": rec.completed_on,
                                     "valid_until": rec.valid_until}
                else:
                    near = (rec.valid_until is not None
                            and (rec.valid_until - today).days <= DUE_SOON_DAYS)
                    cells[c.code] = {"state": "DUE_SOON" if near else "OK",
                                     "completed_on": rec.completed_on,
                                     "valid_until": rec.valid_until}
                    summary[c.id]["ok"] += 1
            items.append({"employee_id": emp.id, "employee_no": emp.employee_no,
                          "name": emp.name, "job_category": emp.job_category, "cells": cells})
        return {"as_of": today,
                "courses": [{"id": c.id, "code": c.code, "title": c.title,
                             "category": c.category, "cycle_months": c.cycle_months}
                            for c in courses],
                "items": items,
                "summary": [{"course_id": c.id, "code": c.code, "title": c.title,
                             "required": summary[c.id]["required"],
                             "compliant": summary[c.id]["ok"],
                             "rate": round(summary[c.id]["ok"] / summary[c.id]["required"], 3)
                             if summary[c.id]["required"] else None} for c in courses]}
