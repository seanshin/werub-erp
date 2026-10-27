"""진료의뢰·회송 (진료협력센터·P3-3) — 의뢰/회송 기록·회송률 등 실적 통계.

referral_type: REFER_OUT(진료의뢰)·REFER_IN(의뢰접수·초진)·BACK_REFER(회송). 의뢰회송료·회송률
(상급종합병원 지정평가·인증 지표 소스). 임상 의뢰서 작성은 진료(HIS), 본 도메인은 실적·수가 관리.
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.patient.models import Referral

TYPES = ("REFER_OUT", "REFER_IN", "BACK_REFER")


class ReferralService:
    async def create(self, session: AsyncSession, *, referral_type: str, partner_institution: str,
                     referred_on: date, chart_no: str | None = None,
                     patient_name: str | None = None, department: str | None = None,
                     reason: str | None = None, has_fee: bool = False,
                     note: str | None = None, actor: int | None = None) -> Referral:
        if referral_type not in TYPES:
            raise ValidationError(f"의뢰유형 오류: {referral_type}")
        if not (partner_institution or "").strip():
            raise ValidationError("상대 의료기관은 필수입니다")
        r = Referral(referral_type=referral_type, partner_institution=partner_institution.strip(),
                     chart_no=chart_no, patient_name=patient_name, department=department,
                     referred_on=referred_on, reason=reason, has_fee=1 if has_fee else 0,
                     note=note, created_by=actor)
        session.add(r)
        await session.flush()
        return r

    async def close(self, session: AsyncSession, *, referral_id: int) -> Referral:
        r = await session.get(Referral, referral_id)
        if r is None:
            raise ValidationError("존재하지 않는 의뢰·회송입니다")
        r.status = "CLOSED"
        await session.flush()
        return r

    async def list_referrals(self, session: AsyncSession, *, referral_type: str | None = None,
                             start: date | None = None, end: date | None = None,
                             status: str | None = None) -> list[dict]:
        q = select(Referral)
        if referral_type:
            q = q.where(Referral.referral_type == referral_type)
        if start:
            q = q.where(Referral.referred_on >= start)
        if end:
            q = q.where(Referral.referred_on <= end)
        if status:
            q = q.where(Referral.status == status)
        rows = (await session.scalars(q.order_by(Referral.referred_on.desc()).limit(1000))).all()
        return [self._dto(r) for r in rows]

    async def stats(self, session: AsyncSession, *, start: date, end: date) -> dict:
        """기간 실적 — 유형별 건수·의뢰회송료 건수·회송률(회송/의뢰접수 근사)."""
        rows = (await session.execute(
            select(Referral.referral_type, func.count(),
                   func.coalesce(func.sum(Referral.has_fee), 0))
            .where(Referral.referred_on >= start, Referral.referred_on <= end)
            .group_by(Referral.referral_type))).all()
        by_type = {t: {"count": 0, "fee": 0} for t in TYPES}
        for t, n, fee in rows:
            by_type[t] = {"count": int(n), "fee": int(fee)}
        refer_in = by_type["REFER_IN"]["count"]
        back = by_type["BACK_REFER"]["count"]
        # 회송률(내부 근사) = 회송 / 의뢰접수(초진). 상급종합 지정평가는 외래초진 분모(HIS) — 별도.
        back_rate = round(back / refer_in * 100, 1) if refer_in else None
        return {"start": str(start), "end": str(end), "by_type": by_type,
                "total": sum(v["count"] for v in by_type.values()),
                "fee_total": sum(v["fee"] for v in by_type.values()),
                "back_refer_rate": back_rate}

    def _dto(self, r: Referral) -> dict:
        return {"id": r.id, "referral_type": r.referral_type,
                "partner_institution": r.partner_institution, "chart_no": r.chart_no,
                "patient_name": r.patient_name, "department": r.department,
                "referred_on": str(r.referred_on), "reason": r.reason,
                "has_fee": bool(r.has_fee), "status": r.status, "note": r.note}
