"""자동 근무표 생성 (FR-45) — 직군 템플릿 기반 듀티 자동 편성.

직원별로 표준 교대 패턴을 오프셋 적용해 커버리지를 분산. 생성 결과는 save_month로
저장(덮어쓰기) + 노동법 룰 평가(주52h·전공의80h·연속N — FR-32). 위반은 경고로 반환,
관리자가 검토·조정 후 publish.

기본 패턴(8일 주기): D D E E N N OFF OFF — 야간 후 휴식(OFF×2) 보장.
"""
import calendar
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.hr.duty_service import DutyCell, DutyService
from erp.hr.models import Employee

DEFAULT_PATTERN = ("D", "D", "E", "E", "N", "N", "OFF", "OFF")


class DutyGenerator:
    def __init__(self, duty_service: DutyService | None = None):
        self._duty = duty_service or DutyService()

    async def generate(self, session: AsyncSession, *, department_id: int, yyyymm: str,
                       job_group: str = "NURSE",
                       pattern: tuple[str, ...] = DEFAULT_PATTERN):
        """대상 직군 직원에게 패턴 오프셋 배정 → save_month. 반환: (schedule, violations, 인원)."""
        emps = (await session.execute(
            select(Employee.id).where(
                Employee.department_id == department_id,
                Employee.job_group == job_group,
                Employee.resigned_on.is_(None)).order_by(Employee.id))).scalars().all()
        if not emps:
            raise ValidationError("자동 편성 대상 직원이 없습니다",
                                  {"department_id": department_id, "job_group": job_group})

        year, month = int(yyyymm[:4]), int(yyyymm[5:7])
        days = calendar.monthrange(year, month)[1]
        cells = [
            DutyCell(employee_id=eid, duty_on=date(year, month, d),
                     shift=pattern[(d - 1 + i) % len(pattern)])
            for i, eid in enumerate(emps) for d in range(1, days + 1)
        ]
        schedule, violations = await self._duty.save_month(
            session, department_id=department_id, yyyymm=yyyymm, cells=cells)
        return schedule, violations, len(emps)
