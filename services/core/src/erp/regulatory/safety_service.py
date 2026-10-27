"""환자안전사고 보고 서비스 (환자안전법 §14) — 접수·상태전이·통계.

자율보고(reporter 익명 가능). REPORTED→REVIEWING→ACTIONED→CLOSED. 검토·종결은 관리자(라우터 scope).
임상 RCA는 임상/HIS, ERP는 보고관리·분류·재발방지 추적·통계.
"""
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.regulatory.models import PatientSafetyIncident

_TRANSITIONS = {"REPORTED": {"REVIEWING"}, "REVIEWING": {"ACTIONED", "CLOSED"},
                "ACTIONED": {"CLOSED"}, "CLOSED": set()}


class PatientSafetyService:
    async def report(self, session: AsyncSession, *, occurred_on: date, incident_type: str,
                     description: str, severity: str = "NONE", category: str | None = None,
                     department_id: int | None = None,
                     reporter_id: int | None = None) -> PatientSafetyIncident:
        if not description or not description.strip():
            raise ValidationError("사고 경위는 필수입니다")
        inc = PatientSafetyIncident(
            occurred_on=occurred_on, incident_type=incident_type, severity=severity,
            category=category, department_id=department_id,
            description=description.strip(), reporter_id=reporter_id, status="REPORTED")
        session.add(inc)
        await session.flush()
        return inc

    async def transition(self, session: AsyncSession, *, incident_id: int, to_status: str,
                         actor: int, action: str | None = None) -> PatientSafetyIncident:
        inc = await session.get(PatientSafetyIncident, incident_id)
        if inc is None:
            raise ValidationError("존재하지 않는 사고 보고입니다")
        if to_status not in _TRANSITIONS.get(inc.status, set()):
            raise ValidationError(f"허용되지 않은 상태 전이: {inc.status}→{to_status}")
        if to_status in ("ACTIONED", "CLOSED") and action:
            inc.action = action.strip()
        inc.status = to_status
        inc.reviewed_by = actor
        inc.reviewed_at = datetime.now(timezone.utc)
        return inc

    async def stats(self, session: AsyncSession, *, since: date, until: date) -> dict:
        """기간 통계 — 유형·중대도·상태별 건수(환자안전위원회 보고용)."""
        rows = (await session.execute(
            select(PatientSafetyIncident.incident_type, PatientSafetyIncident.severity,
                   PatientSafetyIncident.status, func.count())
            .where(PatientSafetyIncident.occurred_on >= since,
                   PatientSafetyIncident.occurred_on <= until)
            .group_by(PatientSafetyIncident.incident_type, PatientSafetyIncident.severity,
                      PatientSafetyIncident.status))).all()
        by_type: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        open_count = total = 0
        for itype, sev, status, n in rows:
            by_type[itype] = by_type.get(itype, 0) + n
            by_severity[sev] = by_severity.get(sev, 0) + n
            total += n
            if status != "CLOSED":
                open_count += n
        return {"since": str(since), "until": str(until), "total": total,
                "open": open_count, "by_type": by_type, "by_severity": by_severity}
