"""감염관리 지표 (의료기관 인증평가·P3-1) — 월별 지표 upsert·비율 산출·추세·목표대비 경보.

지표유형별 비율 = 분자/분모×배수. 감염밀도(HAI_*)=×1000(재원일수), 수행률(HAND_HYGIENE)=×100,
항생제/MDRO는 기관 정책 배수. 목표(target) 초과·근접 시 상태 표시. 임상판정·미생물 결과는 HIS 소관.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.regulatory.models import InfectionIndicator

INDICATOR_TYPES = ("HAI_UTI", "HAI_BSI", "HAI_SSI", "HAI_VAP",
                   "HAND_HYGIENE", "ANTIBIOTIC", "MDRO")
# 방향성 — 대부분 낮을수록 좋음(target 초과=경보), 손위생 수행률만 높을수록 좋음(target 미달=경보)
_HIGHER_BETTER = {"HAND_HYGIENE"}


def _rate(numerator: float, denominator: float, multiplier: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator * multiplier, 2)


def _status(rate: float | None, target: float | None, indicator_type: str) -> str:
    """목표 대비 상태 — GOOD/WARN/ALERT. target 없으면 NONE."""
    if rate is None or target is None:
        return "NONE"
    if indicator_type in _HIGHER_BETTER:          # 높을수록 좋음(수행률)
        if rate >= target:
            return "GOOD"
        return "WARN" if rate >= target * 0.9 else "ALERT"
    # 낮을수록 좋음(감염밀도·MDRO 등)
    if rate <= target:
        return "GOOD"
    return "WARN" if rate <= target * 1.1 else "ALERT"


class InfectionService:
    async def record(self, session: AsyncSession, *, period: str, indicator_type: str,
                     numerator: float, denominator: float, multiplier: int = 1000,
                     department_id: int | None = None, target: float | None = None,
                     note: str | None = None, actor: int | None = None) -> dict:
        """월별 지표 upsert — (period,type,dept) 멱등. 분모>0·음수 방어."""
        if indicator_type not in INDICATOR_TYPES:
            raise ValidationError(f"알 수 없는 지표유형: {indicator_type}")
        if not period or len(period) != 7 or period[4] != "-":
            raise ValidationError("기간은 YYYY-MM 형식이어야 합니다")
        if numerator < 0 or denominator < 0:
            raise ValidationError("분자·분모는 음수일 수 없습니다")
        if denominator == 0:
            raise ValidationError("분모(재원일수·관찰기회 등)는 0보다 커야 합니다")
        row = await session.scalar(select(InfectionIndicator).where(
            InfectionIndicator.period == period,
            InfectionIndicator.indicator_type == indicator_type,
            InfectionIndicator.department_id == department_id))
        if row is None:
            row = InfectionIndicator(period=period, indicator_type=indicator_type,
                                     department_id=department_id, created_by=actor,
                                     numerator=numerator, denominator=denominator)
            session.add(row)
        row.numerator, row.denominator, row.multiplier = numerator, denominator, multiplier
        row.target, row.note = target, note
        await session.flush()
        rate = _rate(numerator, denominator, multiplier)
        return {"id": row.id, "period": period, "indicator_type": indicator_type,
                "rate": rate, "status": _status(rate, float(target) if target is not None else None,
                                                indicator_type)}

    async def list_indicators(self, session: AsyncSession, *, indicator_type: str | None = None,
                              period_from: str | None = None, period_to: str | None = None,
                              department_id: int | None = None) -> list[dict]:
        """지표 목록 — 유형·기간범위·부서 필터. 최신 기간 우선."""
        q = select(InfectionIndicator)
        if indicator_type:
            q = q.where(InfectionIndicator.indicator_type == indicator_type)
        if period_from:
            q = q.where(InfectionIndicator.period >= period_from)
        if period_to:
            q = q.where(InfectionIndicator.period <= period_to)
        if department_id is not None:
            q = q.where(InfectionIndicator.department_id == department_id)
        rows = (await session.scalars(q.order_by(
            InfectionIndicator.period.desc(), InfectionIndicator.indicator_type).limit(1000))).all()
        out = []
        for r in rows:
            rate = _rate(float(r.numerator), float(r.denominator), r.multiplier)
            tgt = float(r.target) if r.target is not None else None
            out.append({"id": r.id, "period": r.period, "indicator_type": r.indicator_type,
                        "department_id": r.department_id, "numerator": float(r.numerator),
                        "denominator": float(r.denominator), "multiplier": r.multiplier,
                        "rate": rate, "target": tgt, "note": r.note,
                        "status": _status(rate, tgt, r.indicator_type)})
        return out

    async def dashboard(self, session: AsyncSession, *, period: str) -> dict:
        """월 대시보드 — 유형별 당월 지표(기관 전체·dept NULL 우선)+경보 건수."""
        rows = await self.list_indicators(session, period_from=period, period_to=period)
        by_type = {}
        for r in rows:
            # 기관 전체(dept NULL)를 대표값으로, 없으면 첫 부서값
            if r["indicator_type"] not in by_type or r["department_id"] is None:
                by_type[r["indicator_type"]] = r
        alerts = [r for r in rows if r["status"] == "ALERT"]
        return {"period": period, "indicators": list(by_type.values()),
                "alert_count": len(alerts),
                "alerts": [{"indicator_type": a["indicator_type"], "rate": a["rate"],
                            "target": a["target"], "department_id": a["department_id"]}
                           for a in alerts]}
