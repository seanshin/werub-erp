"""법정의무교육·직원 건강검진 추적 (인사 P3) — 이수 기록·주기 만료·현황 매트릭스.

필수 항목(의료기관 기준, 운영 조정 가능 상수):
- 교육(연 1회): 성희롱예방·개인정보보호·산업안전보건·장애인식개선
- 검진: 일반(연 1회 — 사무직 2년 단순화 생략), 특수(방사선 종사자 RADIOTECH, 연 1회)
상태: OK | DUE_SOON(만료 30일 전) | OVERDUE | NONE(이수 이력 없음)
"""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.hr.models import ComplianceRecord, Employee

_audit = AuditService()

# category → (kind, 라벨, 유효기간 일수, 대상 판정)
REQUIREMENTS: dict[str, dict] = {
    "SEXUAL_HARASSMENT": {"kind": "TRAINING", "label": "성희롱예방", "days": 365},
    "PRIVACY": {"kind": "TRAINING", "label": "개인정보보호", "days": 365},
    "SAFETY": {"kind": "TRAINING", "label": "산업안전보건", "days": 365},
    "DISABILITY": {"kind": "TRAINING", "label": "장애인식개선", "days": 365},
    "CHECKUP_GENERAL": {"kind": "CHECKUP", "label": "일반건강검진", "days": 365},
    "CHECKUP_SPECIAL": {"kind": "CHECKUP", "label": "특수건강진단(방사선)", "days": 365,
                        "job_categories": {"RADIOTECH"}},
}
DUE_SOON_DAYS = 30


def _applies(category: str, emp: Employee) -> bool:
    jobs = REQUIREMENTS[category].get("job_categories")
    return jobs is None or (emp.job_category or "") in jobs


class ComplianceService:
    async def record(self, session: AsyncSession, *, actor: int, employee_id: int,
                     category: str, done_on: date,
                     note: str | None = None) -> ComplianceRecord:
        req = REQUIREMENTS.get(category)
        if req is None:
            raise ValidationError(f"알 수 없는 항목: {category} "
                                  f"(허용: {', '.join(REQUIREMENTS)})")
        if await session.get(Employee, employee_id) is None:
            raise ValidationError("존재하지 않는 직원입니다")
        existing = await session.scalar(select(ComplianceRecord).where(
            ComplianceRecord.employee_id == employee_id,
            ComplianceRecord.category == category,
            ComplianceRecord.done_on == done_on))
        if existing is not None:
            return existing                       # (직원,항목,이수일) 멱등
        rec = ComplianceRecord(
            employee_id=employee_id, kind=req["kind"], category=category,
            done_on=done_on, valid_until=done_on + timedelta(days=req["days"]),
            note=note, created_by=actor)
        session.add(rec)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="hr.compliance_records", target_id=str(rec.id),
                                  after={"category": category, "done_on": str(done_on)})
        return rec

    async def status(self, session: AsyncSession, *, as_of: date | None = None) -> dict:
        """재직자 × 필수항목 매트릭스 + 항목별 이수율."""
        today = as_of or date.today()
        employees = (await session.scalars(
            select(Employee).where(Employee.resigned_on.is_(None))
            .order_by(Employee.employee_no))).all()
        latest: dict[tuple[int, str], ComplianceRecord] = {}
        for r in (await session.scalars(select(ComplianceRecord)
                                        .order_by(ComplianceRecord.done_on))).all():
            latest[(r.employee_id, r.category)] = r    # done_on 오름차순 → 마지막이 최신

        items, summary = [], {c: {"required": 0, "ok": 0} for c in REQUIREMENTS}
        for emp in employees:
            cells = {}
            for cat in REQUIREMENTS:
                if not _applies(cat, emp):
                    cells[cat] = {"state": "N/A"}
                    continue
                summary[cat]["required"] += 1
                rec = latest.get((emp.id, cat))
                if rec is None:
                    cells[cat] = {"state": "NONE"}
                elif rec.valid_until < today:
                    cells[cat] = {"state": "OVERDUE", "done_on": rec.done_on,
                                  "valid_until": rec.valid_until}
                else:
                    near = (rec.valid_until - today).days <= DUE_SOON_DAYS
                    cells[cat] = {"state": "DUE_SOON" if near else "OK",
                                  "done_on": rec.done_on, "valid_until": rec.valid_until}
                    # 이수율은 '미만료(유효)' 기준 — DUE_SOON 포함(만료 임박이지 미이수 아님)
                    summary[cat]["ok"] += 1
            items.append({"employee_id": emp.id, "employee_no": emp.employee_no,
                          "name": emp.name, "job_category": emp.job_category,
                          "cells": cells})
        return {"as_of": today,
                "categories": [{"key": k, **{x: v for x, v in r.items()
                                             if x in ("kind", "label")}}
                               for k, r in REQUIREMENTS.items()],
                "items": items,
                "summary": [{"category": c, "label": REQUIREMENTS[c]["label"],
                             "required": s["required"], "compliant": s["ok"],
                             "rate": round(s["ok"] / s["required"], 3)
                             if s["required"] else None}
                            for c, s in summary.items()]}
