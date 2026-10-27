"""벤더(공급사) 관리 — 등록심사 상태 생애주기·평가 스코어카드 (구매 거버넌스 심화).

status: PENDING(등록신청)→APPROVED(승인)→SUSPENDED(정지)/TERMINATED(해지). 평가는 기간별 납기·품질·
단가 점수(0~100)→총점(가중 평균), supplier.eval_score에 최근값 반영. is_active는 APPROVED 동기화.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.scm.models import Supplier, VendorEvaluation

_STATUSES = {"PENDING", "APPROVED", "SUSPENDED", "TERMINATED"}
# 상태 전이 가드 — TERMINATED는 종단(해지 벤더 부활 금지·재거래는 신규 등록). 정지↔승인은 왕복 허용.
_TRANSITIONS = {
    "PENDING": {"APPROVED", "TERMINATED"},
    "APPROVED": {"SUSPENDED", "TERMINATED"},
    "SUSPENDED": {"APPROVED", "TERMINATED"},
    "TERMINATED": set(),
}
# 평가 가중치 — 납기 40%·품질 40%·단가 20%
_W = {"delivery": 0.4, "quality": 0.4, "price": 0.2}


class VendorService:
    async def set_status(self, session: AsyncSession, *, supplier_id: int, status: str,
                         actor: int | None = None) -> Supplier:
        if status not in _STATUSES:
            raise ValidationError(f"상태는 {'/'.join(sorted(_STATUSES))} 중 하나여야 합니다")
        sup = await session.get(Supplier, supplier_id)
        if sup is None:
            raise ValidationError("존재하지 않는 공급사입니다")
        if status != sup.status and status not in _TRANSITIONS.get(sup.status, set()):
            raise ValidationError(f"허용되지 않는 공급사 상태 전이입니다 ({sup.status}→{status})")
        sup.status = status
        sup.is_active = status == "APPROVED"   # 발주 가능 여부 동기화
        return sup

    async def evaluate(self, session: AsyncSession, *, supplier_id: int, period: str,
                       delivery: float, quality: float, price: float,
                       note: str | None = None, actor: int | None = None) -> VendorEvaluation:
        sup = await session.get(Supplier, supplier_id)
        if sup is None:
            raise ValidationError("존재하지 않는 공급사입니다")
        for v in (delivery, quality, price):
            if not 0 <= v <= 100:
                raise ValidationError("점수는 0~100 범위여야 합니다")
        total = round(delivery * _W["delivery"] + quality * _W["quality"]
                      + price * _W["price"], 1)
        existing = await session.scalar(select(VendorEvaluation).where(
            VendorEvaluation.supplier_id == supplier_id, VendorEvaluation.period == period))
        if existing is not None:   # 동일 기간 재평가 — 갱신
            existing.delivery_score, existing.quality_score, existing.price_score = \
                delivery, quality, price
            existing.total, existing.note, existing.created_by = total, note, actor
            ev = existing
        else:
            ev = VendorEvaluation(
                supplier_id=supplier_id, period=period, delivery_score=delivery,
                quality_score=quality, price_score=price, total=total, note=note, created_by=actor)
            session.add(ev)
        sup.eval_score = total     # 최근 평가 반영
        return ev

    async def scorecard(self, session: AsyncSession, *, supplier_id: int) -> dict:
        rows = (await session.execute(
            select(VendorEvaluation).where(VendorEvaluation.supplier_id == supplier_id)
            .order_by(VendorEvaluation.period.desc()))).scalars().all()
        return {"supplier_id": supplier_id,
                "evaluations": [{"period": r.period, "delivery": float(r.delivery_score),
                                 "quality": float(r.quality_score), "price": float(r.price_score),
                                 "total": float(r.total), "note": r.note} for r in rows]}
