"""방사선 안전관리 (진단용방사선 안전관리규칙) — 피폭선량 기록·연한도 점검·장비 정기검사 만기.

종사자 연간 피폭선량 한도 50mSv(법정). 분기/월 측정 누적 → 연 합계 한도 초과 경고. 장비 정기검사
만기(next_inspection_on) 임박 추적.
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.regulatory.models import RadiationDose, RadiationEquipment

_ANNUAL_LIMIT_MSV = 50.0   # 연간 유효선량 한도(법정)


class RadiationService:
    async def record_dose(self, session: AsyncSession, *, employee_id: int, period: str,
                          dose_msv: float, measured_on: date, instrument: str | None = None,
                          actor: int | None = None) -> dict:
        if dose_msv < 0:
            raise ValidationError("선량은 음수일 수 없습니다")
        existing = await session.scalar(select(RadiationDose).where(
            RadiationDose.employee_id == employee_id, RadiationDose.period == period))
        if existing is not None:
            existing.dose_msv, existing.instrument, existing.measured_on = \
                dose_msv, instrument, measured_on
            d = existing
        else:
            d = RadiationDose(employee_id=employee_id, period=period, dose_msv=dose_msv,
                              instrument=instrument, measured_on=measured_on, created_by=actor)
            session.add(d)
        await session.flush()
        year = period[:4]
        ytd = float((await session.execute(
            select(func.coalesce(func.sum(RadiationDose.dose_msv), 0))
            .where(RadiationDose.employee_id == employee_id,
                   RadiationDose.period.like(f"{year}%")))).scalar() or 0)
        return {"id": d.id, "ytd_msv": round(ytd, 3), "annual_limit": _ANNUAL_LIMIT_MSV,
                "over_limit": ytd > _ANNUAL_LIMIT_MSV}

    async def inspections_due(self, session: AsyncSession, *, as_of: date,
                              within_days: int = 30) -> dict:
        from datetime import timedelta
        cutoff = as_of + timedelta(days=within_days)
        rows = (await session.execute(
            select(RadiationEquipment).where(RadiationEquipment.next_inspection_on <= cutoff)
            .order_by(RadiationEquipment.next_inspection_on))).scalars().all()
        return {"as_of": str(as_of), "items": [
            {"equipment_no": e.equipment_no, "name": e.name,
             "next_inspection_on": str(e.next_inspection_on),
             "overdue": e.next_inspection_on < as_of} for e in rows]}

    async def doses_by_employee(self, session: AsyncSession, *, year: str) -> dict:
        """연도 피폭선량 — 종사자별 연 누적(YTD)·한도 초과·측정 이력. FR 방사선 안전 화면."""
        rows = (await session.execute(
            select(RadiationDose).where(RadiationDose.period.like(f"{year}%"))
            .order_by(RadiationDose.employee_id, RadiationDose.period))).scalars().all()
        by_emp: dict[int, dict] = {}
        for d in rows:
            g = by_emp.setdefault(d.employee_id, {"employee_id": d.employee_id, "ytd_msv": 0.0,
                                                  "entries": []})
            g["ytd_msv"] = round(g["ytd_msv"] + float(d.dose_msv), 3)
            g["entries"].append({"period": d.period, "dose_msv": float(d.dose_msv),
                                 "measured_on": str(d.measured_on) if d.measured_on else None,
                                 "instrument": d.instrument})
        items = sorted(by_emp.values(), key=lambda g: g["ytd_msv"], reverse=True)
        for g in items:
            g["over_limit"] = g["ytd_msv"] > _ANNUAL_LIMIT_MSV
        return {"year": year, "annual_limit": _ANNUAL_LIMIT_MSV, "items": items}

    async def list_equipment(self, session: AsyncSession, *, as_of: date) -> dict:
        """전 방사선 장비 — 정기검사 만기 상태 포함(만기 임박순)."""
        rows = (await session.execute(
            select(RadiationEquipment)
            .order_by(RadiationEquipment.next_inspection_on))).scalars().all()
        return {"as_of": str(as_of), "items": [
            {"id": e.id, "equipment_no": e.equipment_no, "name": e.name,
             "last_inspected_on": str(e.last_inspected_on) if e.last_inspected_on else None,
             "next_inspection_on": str(e.next_inspection_on),
             "overdue": e.next_inspection_on < as_of} for e in rows]}
