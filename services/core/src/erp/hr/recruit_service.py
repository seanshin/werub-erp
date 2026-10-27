"""채용 ATS (공고·지원자·전형·P3-9) — 공고 등록/마감, 지원자 접수·전형단계 진행, 퍼널 통계.

전형: APPLIED→SCREENING→INTERVIEW→OFFER→HIRED / REJECTED(어느 단계서나). 지원자 개인정보 최소보관.
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.hr.models import JobApplicant, JobPosting

STAGES = ("APPLIED", "SCREENING", "INTERVIEW", "OFFER", "HIRED", "REJECTED")


class RecruitService:
    async def create_posting(self, session: AsyncSession, *, title: str, posted_on: date,
                             department_id: int | None = None, employment_type: str = "REGULAR",
                             headcount: int = 1, close_on: date | None = None,
                             note: str | None = None, actor: int | None = None) -> JobPosting:
        if not (title or "").strip():
            raise ValidationError("공고 제목은 필수입니다")
        if headcount <= 0:
            raise ValidationError("채용 인원은 1 이상이어야 합니다")
        p = JobPosting(title=title.strip(), department_id=department_id,
                       employment_type=employment_type, headcount=headcount,
                       posted_on=posted_on, close_on=close_on, note=note, created_by=actor)
        session.add(p)
        await session.flush()
        return p

    async def set_posting_status(self, session: AsyncSession, *, posting_id: int,
                                 status: str) -> JobPosting:
        if status not in ("OPEN", "CLOSED", "FILLED"):
            raise ValidationError(f"공고 상태 오류: {status}")
        p = await session.get(JobPosting, posting_id)
        if p is None:
            raise ValidationError("존재하지 않는 공고입니다")
        p.status = status
        await session.flush()
        return p

    async def list_postings(self, session: AsyncSession, *, status: str | None = None) -> list[dict]:
        q = select(JobPosting).order_by(JobPosting.id.desc())
        if status:
            q = q.where(JobPosting.status == status)
        rows = (await session.scalars(q.limit(500))).all()
        # 공고별 지원자 수(단일 group-by·N+1 회피)
        ids = [p.id for p in rows]
        cnt = dict((await session.execute(
            select(JobApplicant.posting_id, func.count()).where(JobApplicant.posting_id.in_(ids))
            .group_by(JobApplicant.posting_id))).all()) if ids else {}
        return [{"id": p.id, "title": p.title, "department_id": p.department_id,
                 "employment_type": p.employment_type, "headcount": p.headcount,
                 "status": p.status, "posted_on": str(p.posted_on),
                 "close_on": str(p.close_on) if p.close_on else None, "note": p.note,
                 "applicant_count": int(cnt.get(p.id, 0))} for p in rows]

    async def add_applicant(self, session: AsyncSession, *, posting_id: int, name: str,
                            applied_on: date, contact: str | None = None,
                            source: str | None = None, note: str | None = None,
                            actor: int | None = None) -> JobApplicant:
        p = await session.get(JobPosting, posting_id)
        if p is None:
            raise ValidationError("존재하지 않는 공고입니다")
        if not (name or "").strip():
            raise ValidationError("지원자 이름은 필수입니다")
        a = JobApplicant(posting_id=posting_id, name=name.strip(), contact=contact,
                         source=source, applied_on=applied_on, note=note, created_by=actor)
        session.add(a)
        await session.flush()
        return a

    async def advance(self, session: AsyncSession, *, applicant_id: int, stage: str) -> JobApplicant:
        if stage not in STAGES:
            raise ValidationError(f"전형 단계 오류: {stage}")
        a = await session.get(JobApplicant, applicant_id)
        if a is None:
            raise ValidationError("존재하지 않는 지원자입니다")
        if a.stage in ("HIRED", "REJECTED") and stage != a.stage:
            raise ValidationError(f"종결된 전형({a.stage})은 변경할 수 없습니다")
        a.stage = stage
        await session.flush()
        return a

    async def list_applicants(self, session: AsyncSession, *, posting_id: int) -> list[dict]:
        rows = (await session.scalars(select(JobApplicant).where(
            JobApplicant.posting_id == posting_id)
            .order_by(JobApplicant.id.desc()))).all()
        return [{"id": a.id, "name": a.name, "contact": a.contact, "source": a.source,
                 "stage": a.stage, "applied_on": str(a.applied_on), "note": a.note} for a in rows]

    async def funnel(self, session: AsyncSession, *, posting_id: int) -> dict:
        """공고 전형 퍼널 — 단계별 인원."""
        rows = (await session.execute(
            select(JobApplicant.stage, func.count()).where(JobApplicant.posting_id == posting_id)
            .group_by(JobApplicant.stage))).all()
        by = {s: 0 for s in STAGES}
        for s, n in rows:
            by[s] = int(n)
        return {"posting_id": posting_id, "by_stage": by,
                "total": sum(by.values()), "hired": by["HIRED"]}
