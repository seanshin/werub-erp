"""의약품 회수·의료폐기물 대장 (규제·안전 logging — 법정).

의약품 회수: 회수 등록 → 보유 영향 로트 식별(item+lot, remaining>0). 의료폐기물: 배출·인계 대장.
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.scm.models import DrugRecall, Lot, MedicalWasteLog


class DrugRecallService:
    async def register(self, session: AsyncSession, *, item_id: int, reason: str, recall_on: date,
                       lot_no: str | None = None, severity: str = "VOLUNTARY",
                       actor: int | None = None) -> dict:
        if not reason or not reason.strip():
            raise ValidationError("회수 사유는 필수입니다")
        rec = DrugRecall(item_id=item_id, lot_no=lot_no, reason=reason.strip(),
                         severity=severity, recall_on=recall_on, created_by=actor)
        session.add(rec)
        await session.flush()
        # 보유 영향 로트 식별(격리 대상) — item(+lot) 잔량 > 0
        q = select(Lot).where(Lot.item_id == item_id, Lot.remaining_qty > 0)
        if lot_no:
            q = q.where(Lot.lot_no == lot_no)
        lots = (await session.execute(q)).scalars().all()
        return {"id": rec.id, "affected_lots": [{"lot_no": ln.lot_no,
                                                  "remaining": float(ln.remaining_qty)}
                                                 for ln in lots],
                "affected_qty": round(sum(float(ln.remaining_qty) for ln in lots), 2)}

    async def close(self, session: AsyncSession, *, recall_id: int) -> DrugRecall:
        rec = await session.get(DrugRecall, recall_id)
        if rec is None:
            raise ValidationError("존재하지 않는 회수입니다")
        rec.status = "CLOSED"
        return rec

    async def list_recalls(self, session: AsyncSession, *, status: str | None = None) -> list[dict]:
        """회수 대장 — 최신순. status(OPEN/CLOSED) 필터."""
        q = select(DrugRecall).order_by(DrugRecall.id.desc())
        if status:
            q = q.where(DrugRecall.status == status)
        rows = (await session.execute(q.limit(500))).scalars().all()
        # 영향 로트·수량을 **등록 시점에만** 계산해 토스트로 흘려보내고 목록엔 남기지 않아,
        # OPEN 회수건의 "지금 몇 개를 격리해야 하는지"를 다시 볼 방법이 없었다.
        # 회수는 식약처 대응 법정 업무이고 잔량은 시간에 따라 변하므로 **조회 시점 기준**으로 낸다.
        affected: dict[int, dict] = {}
        if rows:
            lots = (await session.scalars(select(Lot).where(
                Lot.item_id.in_({r.item_id for r in rows}),
                Lot.remaining_qty > 0))).all()
            for r in rows:
                hit = [ln for ln in lots
                       if ln.item_id == r.item_id and (not r.lot_no or ln.lot_no == r.lot_no)]
                affected[r.id] = {
                    "lots": [{"lot_no": ln.lot_no, "remaining": float(ln.remaining_qty)}
                             for ln in hit],
                    "qty": round(sum(float(ln.remaining_qty) for ln in hit), 2)}
        return [{"id": r.id, "item_id": r.item_id, "lot_no": r.lot_no, "reason": r.reason,
                 "severity": r.severity, "recall_on": str(r.recall_on), "status": r.status,
                 "created_by": r.created_by,
                 "affected_lots": affected.get(r.id, {}).get("lots", []),
                 "affected_qty": affected.get(r.id, {}).get("qty", 0.0)}
                for r in rows]


class MedicalWasteService:
    async def record(self, session: AsyncSession, *, waste_type: str, weight_kg: float,
                     stored_on: date, actor: int | None = None) -> MedicalWasteLog:
        if weight_kg <= 0:
            raise ValidationError("중량은 0보다 커야 합니다")
        w = MedicalWasteLog(waste_type=waste_type, weight_kg=weight_kg, stored_on=stored_on,
                            created_by=actor)
        session.add(w)
        await session.flush()
        return w

    async def hand_over(self, session: AsyncSession, *, log_id: int, handed_over_on: date,
                        handler: str, manifest_no: str | None = None) -> MedicalWasteLog:
        w = await session.get(MedicalWasteLog, log_id)
        if w is None:
            raise ValidationError("존재하지 않는 배출 기록입니다")
        w.handed_over_on, w.handler, w.manifest_no = handed_over_on, handler, manifest_no
        w.status = "HANDED_OVER"
        return w

    async def list_logs(self, session: AsyncSession, *, status: str | None = None) -> list[dict]:
        """폐기물 배출대장 — 최신순. status(STORED/HANDED_OVER) 필터."""
        q = select(MedicalWasteLog).order_by(MedicalWasteLog.id.desc())
        if status:
            q = q.where(MedicalWasteLog.status == status)
        rows = (await session.execute(q.limit(500))).scalars().all()
        return [{"id": w.id, "waste_type": w.waste_type, "weight_kg": float(w.weight_kg),
                 "stored_on": str(w.stored_on),
                 "handed_over_on": str(w.handed_over_on) if w.handed_over_on else None,
                 "handler": w.handler, "manifest_no": w.manifest_no, "status": w.status}
                for w in rows]
