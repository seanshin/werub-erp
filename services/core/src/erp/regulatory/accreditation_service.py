"""의료기관 인증평가 조사항목 관리 (상시준비·P3-2) — 조사기준별 준비상태·근거·담당 등록/갱신·요약.

지표(환자안전·감염관리·방사선·VOC)는 각 도메인 화면 연계, 본 서비스는 조사항목 준비상태 등록부.
요약 = 도메인별 상태 카운트 + 준비율(READY/(전체−NA)).
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.regulatory.models import AccreditationItem

DOMAINS = ("BASIC", "CARE", "ADMIN", "PERFORMANCE")
STATUSES = ("READY", "PARTIAL", "GAP", "NA")


class AccreditationService:
    async def create(self, session: AsyncSession, *, domain: str, item_code: str, title: str,
                     standard: str = "ACUTE", chapter: str | None = None,
                     requirement: str | None = None, evidence_note: str | None = None,
                     status: str = "GAP", owner_dept_id: int | None = None,
                     note: str | None = None, actor: int | None = None) -> AccreditationItem:
        if domain not in DOMAINS:
            raise ValidationError(f"인증 영역 오류: {domain}")
        if status not in STATUSES:
            raise ValidationError(f"상태 오류: {status}")
        if not (item_code or "").strip() or not (title or "").strip():
            raise ValidationError("조사항목 번호·제목은 필수입니다")
        dup = await session.scalar(select(AccreditationItem).where(
            AccreditationItem.standard == standard, AccreditationItem.item_code == item_code))
        if dup is not None:
            raise ValidationError(f"이미 등록된 조사항목: {standard}/{item_code}")
        it = AccreditationItem(standard=standard, domain=domain, chapter=chapter,
                               item_code=item_code, title=title, requirement=requirement,
                               evidence_note=evidence_note, status=status,
                               owner_dept_id=owner_dept_id, note=note, created_by=actor)
        session.add(it)
        await session.flush()
        return it

    async def update(self, session: AsyncSession, *, item_id: int, status: str | None = None,
                     evidence_note: str | None = None, owner_dept_id: int | None = None,
                     requirement: str | None = None, note: str | None = None,
                     reviewed: bool = False) -> AccreditationItem:
        it = await session.get(AccreditationItem, item_id)
        if it is None:
            raise ValidationError("존재하지 않는 조사항목입니다")
        if status is not None:
            if status not in STATUSES:
                raise ValidationError(f"상태 오류: {status}")
            it.status = status
        if evidence_note is not None:
            it.evidence_note = evidence_note
        if owner_dept_id is not None:
            it.owner_dept_id = owner_dept_id
        if requirement is not None:
            it.requirement = requirement
        if note is not None:
            it.note = note
        if reviewed or status is not None:
            it.last_reviewed_on = date.today()
        await session.flush()
        return it

    async def list_items(self, session: AsyncSession, *, domain: str | None = None,
                         status: str | None = None, standard: str = "ACUTE") -> list[dict]:
        q = select(AccreditationItem).where(AccreditationItem.standard == standard)
        if domain:
            q = q.where(AccreditationItem.domain == domain)
        if status:
            q = q.where(AccreditationItem.status == status)
        rows = (await session.scalars(q.order_by(
            AccreditationItem.domain, AccreditationItem.item_code).limit(2000))).all()
        # 책임부서를 원시 PK(#12)로 찍으면 담당을 특정할 수 없다 — 이름으로 해소해 함께 반환.
        from erp.common.models import Department
        dept_ids = {r.owner_dept_id for r in rows if r.owner_dept_id is not None}
        names = dict((await session.execute(
            select(Department.id, Department.name).where(Department.id.in_(dept_ids)))).all()
        ) if dept_ids else {}
        return [self._dto(r) | {"owner_dept_name": names.get(r.owner_dept_id)} for r in rows]

    async def summary(self, session: AsyncSession, *, standard: str = "ACUTE") -> dict:
        """도메인별 상태 카운트 + 준비율(READY/(전체−NA))."""
        rows = (await session.execute(
            select(AccreditationItem.domain, AccreditationItem.status, func.count())
            .where(AccreditationItem.standard == standard)
            .group_by(AccreditationItem.domain, AccreditationItem.status))).all()
        by_domain: dict[str, dict] = {d: {s: 0 for s in STATUSES} for d in DOMAINS}
        for domain, status, n in rows:
            by_domain.setdefault(domain, {s: 0 for s in STATUSES})[status] = n
        out = []
        tot_ready = tot_scored = 0
        for d in DOMAINS:
            c = by_domain[d]
            scored = c["READY"] + c["PARTIAL"] + c["GAP"]     # NA 제외
            rate = round(c["READY"] / scored * 100, 1) if scored else None
            tot_ready += c["READY"]
            tot_scored += scored
            out.append({"domain": d, **c, "total": scored + c["NA"], "ready_rate": rate})
        overall = round(tot_ready / tot_scored * 100, 1) if tot_scored else None
        return {"standard": standard, "domains": out, "overall_ready_rate": overall,
                "total_items": sum(v["total"] for v in out)}

    def _dto(self, r: AccreditationItem) -> dict:
        return {"id": r.id, "standard": r.standard, "domain": r.domain, "chapter": r.chapter,
                "item_code": r.item_code, "title": r.title, "requirement": r.requirement,
                "evidence_note": r.evidence_note, "status": r.status,
                "owner_dept_id": r.owner_dept_id,
                "last_reviewed_on": str(r.last_reviewed_on) if r.last_reviewed_on else None,
                "note": r.note}
