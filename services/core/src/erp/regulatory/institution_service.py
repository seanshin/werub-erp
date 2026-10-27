"""기관 규제 프로필 + 분기 재원환자수 대장 서비스 — 간호등급 등 산정 입력 정보 관리.

싱글턴 프로필(OrgSettings 패턴) + 다중행 census(Holiday 패턴). 산정 엔진은 완성돼 있고, 여기서
관리자가 설정한 종별·허가병상·분기 실측 재원일수를 공급한다(HIS 0시 census M0a 도착 전 수동 소스).
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.regulatory.models import InstitutionProfile, NursingCensus

_audit = AuditService()

_HOSPITAL_TYPES = {"TERTIARY", "GENERAL", "HOSPITAL", "CLINIC_BED_BASIS"}


class InstitutionProfileService:
    async def get(self, session: AsyncSession) -> InstitutionProfile | None:
        return await session.get(InstitutionProfile, 1)

    async def effective(self, session: AsyncSession) -> dict:
        """미설정 시 안전 기본값(HOSPITAL). source=db/default."""
        row = await self.get(session)
        return {"hospital_type": row.hospital_type if row else "HOSPITAL",
                "licensed_beds": row.licensed_beds if row else None,
                "updated_at": row.updated_at if row else None,
                "source": "db" if row else "default"}

    async def upsert(self, session: AsyncSession, *, actor: int,
                     hospital_type: str | None = None,
                     licensed_beds: int | None = None) -> InstitutionProfile:
        if hospital_type is not None and hospital_type not in _HOSPITAL_TYPES:
            raise ValidationError(f"종별 오류: {hospital_type}")
        if licensed_beds is not None and licensed_beds < 0:
            raise ValidationError("허가병상수는 0 이상이어야 합니다")
        row = await self.get(session)
        if row is None:
            # 싱글턴(id=1) 동시 최초삽입 경합 — PK 충돌 시 500 대신 재조회(적대검토 LOW). 신규 행은
            # 모든 필드를 채운 단일 INSERT(후속 UPDATE 없음 → updated_at onupdate 미발화·조회 안전).
            try:
                async with session.begin_nested():
                    row = InstitutionProfile(id=1, hospital_type=hospital_type or "HOSPITAL",
                                             licensed_beds=licensed_beds, updated_by=actor)
                    session.add(row)
                    await session.flush()
                return row
            except IntegrityError:
                row = await self.get(session)
        if hospital_type is not None:
            row.hospital_type = hospital_type
        if licensed_beds is not None:
            row.licensed_beds = licensed_beds
        row.updated_by = actor
        await session.flush()
        return row


class NursingCensusService:
    async def upsert(self, session: AsyncSession, *, actor: int, period_start: date,
                     period_end: date, patient_days: float,
                     note: str | None = None) -> NursingCensus:
        if period_end < period_start:
            raise ValidationError("기간 종료일이 시작일보다 빠릅니다")
        if patient_days < 0:
            raise ValidationError("재원일수는 0 이상이어야 합니다")
        row = await session.scalar(select(NursingCensus).where(
            NursingCensus.period_start == period_start))
        if row is None:
            # SELECT-then-INSERT 경합 — 유니크(period_start) 충돌 시 500 대신 재조회해 갱신 경로로
            # (동시 관리자 입력·적대검토 LOW). 세이브포인트로 부분 롤백(외부 트랜잭션 보존).
            try:
                async with session.begin_nested():
                    row = NursingCensus(period_start=period_start, period_end=period_end,
                                        patient_days=patient_days, note=note, entered_by=actor)
                    session.add(row)
                    await session.flush()
                return row
            except IntegrityError:
                row = await session.scalar(select(NursingCensus).where(
                    NursingCensus.period_start == period_start))
        row.period_end = period_end
        row.patient_days = patient_days
        row.note = note
        row.entered_by = actor
        await session.flush()
        return row

    async def get_for(self, session: AsyncSession, *, period_start: date) -> NursingCensus | None:
        return await session.scalar(select(NursingCensus).where(
            NursingCensus.period_start == period_start))

    async def list(self, session: AsyncSession, *, limit: int = 12) -> list[NursingCensus]:
        return list((await session.scalars(select(NursingCensus)
                     .order_by(NursingCensus.period_start.desc()).limit(limit))).all())

    async def delete(self, session: AsyncSession, *, census_id: int) -> None:
        row = await session.get(NursingCensus, census_id)
        if row is None:
            raise ValidationError("존재하지 않는 재원환자수 항목입니다")
        await session.delete(row)


async def set_nurse_attributes(session: AsyncSession, *, actor: int, employee_id: int,
                               weekly_hours: float | None,
                               nursing_exclude_reason: str | None):
    """간호사 주 근로시간(FTE 환산)·산정 제외 사유 설정 — 간호등급 정밀화."""
    from erp.hr.models import Employee
    emp = await session.get(Employee, employee_id)
    if emp is None or emp.job_category != "NURSE":
        raise ValidationError("간호사 직원이 아닙니다")
    if weekly_hours is not None and not (0 <= weekly_hours <= 80):
        raise ValidationError("주 근로시간은 0~80 범위여야 합니다")
    before = {"weekly_hours": float(emp.weekly_hours) if emp.weekly_hours is not None else None,
              "nursing_exclude_reason": emp.nursing_exclude_reason}
    emp.weekly_hours = weekly_hours
    emp.nursing_exclude_reason = (nursing_exclude_reason or "").strip() or None
    # 크로스도메인 쓰기(regulatory→hr.employees)라 감사 남김(적대검토 LOW — 감사 부재)
    await _audit.record_audit(session, actor=actor, action="UPDATE",
                              entity="hr.employees", target_id=str(employee_id),
                              before=before,
                              after={"weekly_hours": weekly_hours,
                                     "nursing_exclude_reason": emp.nursing_exclude_reason})
    await session.flush()
    return emp
