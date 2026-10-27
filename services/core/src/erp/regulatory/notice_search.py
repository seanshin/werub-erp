"""공시 전문검색 (AI 사전심사 P0 — RAG의 Retrieval). LLM 없이 PostgreSQL 텍스트 매칭.

청구 코드/상병/키워드로 진료일 시점 유효 공시(고시·심사기준)를 검색·스니펫. 실 공시 적재 시
바로 유효하며, 추후 임베딩/LLM 평가(Generation)를 이 검색 위에 얹는다.
"""
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.regulatory.models import Notice, NoticeRevision


def make_snippet(text: str, terms: list[str], width: int = 140) -> str:
    """첫 매칭 term 주변 발췌."""
    if not text:
        return ""
    low = text.lower()
    pos = min((low.find(t.lower()) for t in terms if t.lower() in low), default=-1)
    if pos < 0:
        return text[:width].strip()
    start = max(0, pos - width // 3)
    seg = text[start:start + width].strip().replace("\n", " ")
    return ("…" if start > 0 else "") + seg + ("…" if start + width < len(text) else "")


class NoticeSearchService:
    async def search(self, session: AsyncSession, *, q: str, as_of: date | None = None,
                     notice_type: str | None = None, limit: int = 20) -> list[dict]:
        """키워드 OR 매칭 + 진료일 시점 유효(temporal) + 매칭수 스코어 정렬."""
        terms = [t for t in q.split() if t][:6]
        if not terms:
            return []
        stmt = (select(NoticeRevision, Notice.notice_type)
                .join(Notice, Notice.erp_ref_id == NoticeRevision.erp_ref_id)
                .where(or_(*[or_(NoticeRevision.body_text.ilike(f"%{t}%"),
                                NoticeRevision.title.ilike(f"%{t}%")) for t in terms])))
        if as_of is not None:                       # 진료일 시점 유효 revision (EXCLUDE로 1건 보장)
            stmt = stmt.where(NoticeRevision.effective_from <= as_of,
                              or_(NoticeRevision.effective_to.is_(None),
                                  NoticeRevision.effective_to >= as_of))
        if notice_type:
            stmt = stmt.where(Notice.notice_type == notice_type)
        rows = (await session.execute(stmt.limit(limit * 3))).all()

        seen: dict = {}
        for rev, ntype in rows:
            hay = f"{rev.title or ''}\n{rev.body_text or ''}".lower()
            score = sum(1 for t in terms if t.lower() in hay)
            cur = seen.get(rev.erp_ref_id)
            if cur is None or score > cur["score"] or rev.revision_no > cur["_rev"]:
                seen[rev.erp_ref_id] = {
                    "erp_ref_id": str(rev.erp_ref_id), "notice_type": ntype,
                    "title": rev.title, "revision_no": rev.revision_no,
                    "effective_from": rev.effective_from, "score": score,
                    "snippet": make_snippet(rev.body_text or "", terms), "_rev": rev.revision_no}
        results = sorted(seen.values(), key=lambda r: -r["score"])[:limit]
        for r in results:
            r.pop("_rev", None)
        return results
