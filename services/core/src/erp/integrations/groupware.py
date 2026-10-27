"""그룹웨어(Clinic) 근태 수신 어댑터 — M0a=REST 확정 (design §5.1).

Clinic(clinic.example.org)의 `GET /api/v1/clinic/attendance`를 호출해 출퇴근 레코드를
integration.groupware_attendance_staging에 멱등 적재. 코어 직접 쓰기 금지 (§5.2) —
집계는 scheduler.job_attendance_summary가 staging을 소비하여 hr.attendance_summaries로.

보안: ai_publisher와 동일 — TLS verify 강제, 발신 도메인 화이트리스트(SSRF 차단),
인증 헤더는 Bearer + X-Hospital-Code (연동 스펙 CLINIC-HIS-API-REFERENCE §인증).
"""
import logging
from collections.abc import Iterator
from datetime import date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.integrations.egress import allowed_hosts as _egress_hosts
from erp.hr.models import GroupwareAttendanceStaging
from erp.integrations.sources import RawAttendanceDTO

logger = logging.getLogger("erp.integrations.groupware")

# SSRF 화이트리스트 — 사용자 입력으로 대상 URL 결정 금지 (ai_publisher와 동일 원칙)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # ISO 8601 (Z 포함) — fromisoformat은 3.11+에서 'Z' 처리
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class RestGroupwareAttendanceSource:
    """Clinic REST 구현체 (GroupwareAttendanceSource Protocol).

    동기 시그니처 유지(Protocol) — 배치 잡(진료시간 외)에서 호출되므로 httpx.Client 사용.
    """

    def __init__(self, base_url: str, api_key: str, hospital_code: str,
                 timeout: float = 15.0, allowed_hosts: frozenset[str] | None = None,
                 transport: httpx.BaseTransport | None = None):
        host = httpx.URL(base_url).host
        if host not in (allowed_hosts or _egress_hosts("clinic")):
            raise ValueError(f"허용되지 않은 그룹웨어 대상: {host}")  # SSRF 방어
        self._base = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "X-Hospital-Code": hospital_code,
        }
        self._timeout = timeout  # httpx 기본 verify=True — 끄지 않는다
        self._transport = transport  # 테스트 주입용 (MockTransport)

    def fetch_attendance(self, since: datetime) -> Iterator[RawAttendanceDTO]:
        with httpx.Client(headers=self._headers, timeout=self._timeout,
                          transport=self._transport) as client:
            # 실경로: Clinic /api/clinic/his/attendance, 응답 {records:[...]} (HIS clinic-api.service 기준)
            resp = client.get(f"{self._base}/api/clinic/his/attendance",
                              params={"since": since.isoformat()})
            resp.raise_for_status()
            body = resp.json()
            records = body.get("records") or (body.get("data") or {}).get("records") or []
        for rec in records:
            dto = self._to_dto(rec)
            if dto is not None:
                yield dto

    @staticmethod
    def _to_dto(rec: dict) -> RawAttendanceDTO | None:
        user_id = rec.get("userId")
        if not user_id:
            return None  # 식별자 없는 레코드는 스킵 (대사 불가)
        clock_in = _parse_dt(rec.get("checkedInAt"))
        clock_out = _parse_dt(rec.get("checkedOutAt"))
        anchor = clock_in or clock_out
        work_on = anchor.date() if anchor else date.today()
        return RawAttendanceDTO(
            # Clinic 근태 엔드포인트는 employeeNo 미제공 — staff-sync로 채워지면 우선 사용,
            # 없으면 userId를 잠정 키로(소비 단계에서 hr.employees.employee_no 대조).
            employee_no=rec.get("employeeNo") or user_id,
            clock_in=clock_in, clock_out=clock_out, work_on=work_on,
            external_user_id=user_id, name=rec.get("name"), department=rec.get("department"),
        )


class GroupwareAttendanceCollector:
    """수신 → 스테이징 멱등 적재 (file_drop.FileDropCollector와 동일 구조)."""

    def __init__(self, source):
        self._source = source

    async def collect(self, session: AsyncSession, *, since: datetime,
                      limit: int = 1000) -> dict:
        collected = duplicate = 0
        for dto in self._source.fetch_attendance(since):
            if collected + duplicate >= limit:
                break
            source_ref = f"{dto.external_user_id or dto.employee_no}:{dto.work_on.isoformat()}"
            exists = await session.scalar(
                select(GroupwareAttendanceStaging.id)
                .where(GroupwareAttendanceStaging.source_ref == source_ref))
            if exists is not None:
                duplicate += 1
                continue
            session.add(GroupwareAttendanceStaging(
                source_ref=source_ref, external_user_id=dto.external_user_id or dto.employee_no,
                employee_no=dto.employee_no, name=dto.name, department=dto.department,
                clock_in=dto.clock_in, clock_out=dto.clock_out, work_on=dto.work_on,
                status="NEW"))
            collected += 1
        if collected or duplicate:
            logger.info("그룹웨어 근태 수집", extra={"ctx": {
                "collected": collected, "duplicates": duplicate}})
            # 경계 통과 감사(Phase0 G3) — 직원PII pull 불변 트레일(변동 있을 때만)
            from erp.audit.service import AuditService
            await AuditService().record_audit(
                session, actor=None, action="SYNC", entity="integrations.groupware_attendance",
                after={"collected": collected, "duplicates": duplicate})
        return {"collected": collected, "duplicates": duplicate}
