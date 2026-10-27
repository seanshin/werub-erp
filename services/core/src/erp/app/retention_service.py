"""데이터 보존연한 파기 대상 식별 (design §6.5) — 법정 보존연한 경과 데이터 식별(읽기 전용).

전체검토 규제 갈래 — 기존 job_retention_sweep은 TODO 스텁이었고 보존연한 수치도 부정확(환자 5y).
본 서비스는 보존연한 정책(법정근거)별 **파기 대상 건수만 식별**(COUNT). 실제 파기는 관리자 승인
후 별도 수행(불가역이라 자동삭제 금지). ⚠ 보존연한 수치는 법무 검토 권장(현재 통용값·근거 라벨).
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# (카테고리, 보존연한(년), 법정근거) — 기산 컬럼은 _count에서 카테고리별 지정.
RETENTION_POLICY = [
    ("진료비 라인(진료기록)", 10, "의료법 §22·시행규칙 §15(진료기록 10년)"),
    ("수납기록", 10, "의료법·상법(장부 10년)"),
    ("접속기록(access_log)", 5, "개인정보보호법·의료법(접속기록 보존)"),
    ("감사로그(audit_log)", 5, "내부통제(ICFR)"),
    ("퇴직 직원 인사기록", 3, "근로기준법 §42(3년)"),
]


def _cutoff(as_of: date, years: int) -> date:
    return as_of.replace(year=as_of.year - years)


class RetentionService:
    async def identify_candidates(self, session: AsyncSession, *, as_of: date) -> dict:
        """보존연한 경과 파기 대상 식별 — 카테고리별 건수(읽기 전용·삭제 없음)."""
        from erp.audit.models import AccessLog, AuditLog
        from erp.hr.models import Employee
        from erp.patient.models import BillingLine, Payment

        async def _count(stmt) -> int:
            return int((await session.execute(stmt)).scalar_one() or 0)

        c10 = _cutoff(as_of, 10)
        c5 = _cutoff(as_of, 5)
        c3 = _cutoff(as_of, 3)
        counts = {
            "진료비 라인(진료기록)": await _count(
                select(func.count()).select_from(BillingLine).where(BillingLine.treated_on < c10)),
            "수납기록": await _count(
                select(func.count()).select_from(Payment).where(Payment.paid_on < c10)),
            "접속기록(access_log)": await _count(
                select(func.count()).select_from(AccessLog).where(AccessLog.at < c5)),
            "감사로그(audit_log)": await _count(
                select(func.count()).select_from(AuditLog).where(AuditLog.at < c5)),
            "퇴직 직원 인사기록": await _count(
                select(func.count()).select_from(Employee).where(
                    Employee.resigned_on.isnot(None), Employee.resigned_on < c3)),
        }
        items = [{"category": cat, "retention_years": yrs, "legal_basis": basis,
                  "cutoff": str(_cutoff(as_of, yrs)), "candidates": counts.get(cat, 0)}
                 for cat, yrs, basis in RETENTION_POLICY]
        return {"as_of": str(as_of), "items": items,
                "total_candidates": sum(i["candidates"] for i in items),
                "note": "식별만 — 실제 파기는 관리자 승인 후 별도 수행(불가역). 보존연한은 법무 검토 권장."}
