"""Clinic 직원 동기화 어댑터 (FR-28) — design §5.1.

WeRU.B Clinic(그룹웨어)의 `GET /api/clinic/his/staff`로 직원 마스터를 수신해 ERP
hr.employees에 동기화. Clinic id를 external_user_id로 보존 → 향후 근태(userId) 매핑 토대.

보안(ai_publisher 동일): TLS verify 강제, 도메인 화이트리스트(SSRF), Bearer +
X-Hospital-Code 인증.
"""
import logging
from collections.abc import Iterator
from datetime import date

import httpx

from erp.integrations.egress import allowed_hosts as _egress_hosts
from erp.integrations.sources import RawStaffDTO

logger = logging.getLogger("erp.integrations.clinic_staff")



def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


class RestClinicStaffSource:
    """Clinic REST 구현체 — 동기(배치). 실경로 `/api/clinic/his/staff` (HIS clinic-api.service 기준)."""

    def __init__(self, base_url: str, api_key: str, hospital_code: str,
                 timeout: float = 30.0, allowed_hosts: frozenset[str] | None = None,
                 transport: httpx.BaseTransport | None = None):
        host = httpx.URL(base_url).host
        if host not in (allowed_hosts or _egress_hosts("clinic")):
            raise ValueError(f"허용되지 않은 그룹웨어 대상: {host}")  # SSRF 방어
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}",
                         "X-Hospital-Code": hospital_code}
        self._timeout = timeout
        self._transport = transport

    def fetch_staff(self) -> Iterator[RawStaffDTO]:
        with httpx.Client(headers=self._headers, timeout=self._timeout,
                          transport=self._transport) as client:
            resp = client.get(f"{self._base}/api/clinic/his/staff")
            resp.raise_for_status()
            staff = resp.json().get("staff") or []
        for s in staff:
            ext = s.get("id")
            if not ext:
                continue
            yield RawStaffDTO(
                external_user_id=str(ext),
                name=s.get("username") or s.get("email") or str(ext),
                email=s.get("email"), his_role=s.get("hisRole"),
                department=s.get("department"), license_no=s.get("licenseNumber"),
                joined_on=_date(s.get("joinedAt")))
