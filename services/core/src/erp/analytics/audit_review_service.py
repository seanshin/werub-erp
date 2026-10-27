"""감사 후보 검토 상태 관리 (ADR D-7) — 검토완료 표시·취소·후보별 상태 조회.

검토는 기록 행위 → reviewed_by/at 보존. 후보 자체는 실시간 계산이므로 finding_key로 연결.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.finance.models import AuditReview

_TYPES = {"ABNORMAL", "DUPLICATE"}
_DISPOSITIONS = {"NORMAL", "ACTION"}


class AuditReviewService:
    async def upsert(self, session: AsyncSession, *, finding_type: str, finding_key: str,
                     disposition: str, note: str | None = None,
                     actor: int | None = None) -> AuditReview:
        """후보를 검토완료 처리(동일 finding 재처리 시 갱신) — disposition 필수."""
        if finding_type not in _TYPES:
            raise ValueError(f"finding_type은 {_TYPES} 중 하나여야 합니다")
        if disposition not in _DISPOSITIONS:
            raise ValueError(f"disposition은 {_DISPOSITIONS} 중 하나여야 합니다")
        now = datetime.now(timezone.utc)
        existing = await session.scalar(
            select(AuditReview).where(AuditReview.finding_type == finding_type,
                                      AuditReview.finding_key == finding_key))
        if existing is not None:
            existing.status = "REVIEWED"
            existing.disposition = disposition
            existing.note = note
            existing.reviewed_by = actor
            existing.reviewed_at = now
            return existing
        review = AuditReview(finding_type=finding_type, finding_key=finding_key,
                             status="REVIEWED", disposition=disposition, note=note,
                             reviewed_by=actor, reviewed_at=now)
        session.add(review)
        await session.flush()
        return review

    async def delete(self, session: AsyncSession, *, review_id: int) -> bool:
        """검토 취소 — 후보가 다시 미검토로 돌아감. 없으면 False."""
        review = await session.get(AuditReview, review_id)
        if review is None:
            return False
        await session.delete(review)
        await session.flush()
        return True

    async def map_for(self, session: AsyncSession, *, finding_type: str,
                      keys: list[str]) -> dict[str, dict]:
        """finding_key → 검토상태 맵 (후보 목록에 부착용)."""
        if not keys:
            return {}
        rows = (await session.scalars(
            select(AuditReview).where(AuditReview.finding_type == finding_type,
                                      AuditReview.finding_key.in_(keys)))).all()
        return {r.finding_key: {
            "id": r.id, "status": r.status, "disposition": r.disposition, "note": r.note,
            "reviewed_by": r.reviewed_by,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        } for r in rows}
