"""공휴일 캘린더 서비스(P3) — 변동 공휴일(음력·대체) DB 관리·조회.

고정 양력·주말은 claims/deadline.py 코드가 처리, 본 서비스는 매년 갱신되는 변동 공휴일을 DB로
관리한다. deadline.is_holiday/appeal_deadline에 DB 날짜(extra_dates)를 주입해 코드 하드코딩을 대체
(주입 없으면 코드 EXTRA_HOLIDAYS 폴백 — 무회귀).
"""
from datetime import date

from sqlalchemy import extract, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.models import Holiday
from erp.errors import ValidationError

_KINDS = ("PUBLIC", "SUBSTITUTE", "LUNAR", "TEMP")


class HolidayService:
    async def dates(self, session: AsyncSession, *, year: int | None = None) -> set[date]:
        """DB 등록 공휴일 날짜 집합(변동분). deadline 계산 주입용."""
        q = select(Holiday.on_date)
        if year is not None:
            q = q.where(extract("year", Holiday.on_date) == year)
        return {d for (d,) in (await session.execute(q))}

    async def list(self, session: AsyncSession, *, year: int | None = None) -> list[dict]:
        q = select(Holiday).order_by(Holiday.on_date)
        if year is not None:
            q = q.where(extract("year", Holiday.on_date) == year)
        return [{"id": h.id, "on_date": h.on_date.isoformat(), "name": h.name, "kind": h.kind}
                for h in (await session.scalars(q)).all()]

    async def add(self, session: AsyncSession, *, on_date: date, name: str,
                  kind: str = "PUBLIC", actor: int | None = None) -> Holiday:
        if not name or not name.strip():
            raise ValidationError("공휴일 명칭은 필수입니다")
        if kind not in _KINDS:
            raise ValidationError(f"종류는 {'/'.join(_KINDS)} 중 하나여야 합니다")
        existing = await session.scalar(select(Holiday).where(Holiday.on_date == on_date))
        if existing is not None:
            existing.name, existing.kind, existing.created_by = name.strip(), kind, actor
            return existing
        h = Holiday(on_date=on_date, name=name.strip(), kind=kind, created_by=actor)
        session.add(h)
        await session.flush()
        return h

    async def delete(self, session: AsyncSession, *, holiday_id: int) -> bool:
        h = await session.get(Holiday, holiday_id)
        if h is None:
            return False
        await session.delete(h)
        await session.flush()
        return True


async def candidates(session: AsyncSession, *, year: int) -> dict:
    """그 해 공휴일 후보 + 등록 여부 + 검토 기록(없으면 None)."""
    from erp.common import holiday_candidates as hc
    from erp.common.models import HolidayCandidateReview
    registered = await HolidayService().dates(session, year=year)
    rv = await session.get(HolidayCandidateReview, year)
    return {"year": year, "source": hc.SOURCE,
            "items": [{**c, "on_date": c["on_date"].isoformat(), "registered": c["on_date"] in registered}
                      for c in hc.for_year(year)],
            "review": None if rv is None else {"registered": rv.registered, "excluded": rv.excluded,
                                               "source_note": rv.source_note, "reviewed_by": rv.reviewed_by,
                                               "reviewed_at": rv.reviewed_at}}


async def review_candidates(session: AsyncSession, *, year: int, register: list[date], source_note: str,
                            actor: int) -> dict:
    """후보 검토 확정 — 고른 날짜를 공휴일로 등록하고, 고르지 않은 후보는 '뺀 날짜'로 근거와 함께 남긴다(감사).

    다시 검토하면 기록을 갱신한다(이미 등록된 공휴일은 지우지 않는다 — 삭제는 캘린더에서 건별로).
    """
    from erp.audit.service import AuditService
    from erp.common import holiday_candidates as hc
    from erp.common.models import HolidayCandidateReview
    note = (source_note or "").strip()
    if len(note) < 8:
        raise ValidationError("대조 근거(예: '우주항공청 2027 월력요항 원문 대조')를 8자 이상 입력하세요")
    cands = {c["on_date"]: c for c in hc.for_year(year)}
    if not cands:
        raise ValidationError(f"{year}년 공휴일 후보가 없습니다")
    bad = [d.isoformat() for d in register if d not in cands]
    if bad:
        raise ValidationError("후보에 없는 날짜입니다 — 후보 밖 공휴일은 캘린더에서 직접 추가하세요", {"dates": bad})
    existing = await HolidayService().dates(session, year=year)
    pick = set(register) | (existing & set(cands))         # 이미 등록된 후보는 '뺀 날짜'가 아니다(감사와 달력 일치)
    svc = HolidayService()
    for d in sorted(pick - existing):                      # 이미 있는 공휴일은 덮지 않는다(수기 등록 명칭·등록자 보존)
        c = cands[d]
        await svc.add(session, on_date=d, name=c["name"], kind=c["kind"], actor=actor)
    rv = await session.get(HolidayCandidateReview, year)
    before = None if rv is None else {"registered": rv.registered, "excluded": rv.excluded}
    reg = sorted(d.isoformat() for d in pick)
    exc = sorted(d.isoformat() for d in cands if d not in pick)
    if rv is None:
        rv = HolidayCandidateReview(year=year, registered=reg, excluded=exc, source_note=note[:500], reviewed_by=actor)
        session.add(rv)
    else:
        rv.registered, rv.excluded, rv.source_note, rv.reviewed_by = reg, exc, note[:500], actor
        from erp.common.kst import kst_now
        rv.reviewed_at = kst_now()
    await AuditService().record_audit(session, actor=actor, action="REVIEW", entity="common.holiday_candidate_reviews",
                                      target_id=str(year), before=before,
                                      after={"registered": reg, "excluded": exc, "source_note": note[:300]})
    await session.flush()
    return {"year": year, "registered": reg, "excluded": exc}
