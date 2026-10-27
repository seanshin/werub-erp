"""사번 매핑 백필 — 양방향 (HIS 확정 (나)·2026-07-24 / B-2 역방향 2026-07-29).

방향 2개를 같은 디렉터리 EP 하나로 처리한다.
  ① `backfill()`            ERP → HIS : HIS의 erpEmployeeCode를 채운다(POST map, HIS 쓰기)
  ② `backfill_external_ids()` HIS → ERP : ERP의 external_user_id를 채운다(**조회만**, HIS 쓰기 없음)

HIS 확정: 디렉터리 EP(GET /integration/hr/staff)가 row별 employeeNumber+clinicUserId 제공(90/90).
ERP는 Clinic external_user_id ↔ 디렉터리 clinicUserId 조인 → 해당 직원 employeeNumber 획득 →
POST /integration/hr/staff/:employeeNumber/map 으로 ERP employee_no를 erpEmployeeCode로 백필.
HIS resolver는 OR[erpEmployeeCode, employeeNumber] 양키 해소 — HIS 신규 작업 0.

서명자(의료계약 대상) 우선. SSRF 호스트 고정·X-Integration-Key.

⚠ **키를 읽기/쓰기로 구분한다**(HIS 스펙): 디렉터리 **조회 = piiReadKey**,
map **쓰기 = webhookKey**. 종전에는 둘 다 `pii_read_key or webhook_key`를 써서 쓰기가
401(연동 키가 올바르지 않습니다)로 전건 실패했다. 스로틀(429)이 먼저 떠서 한동안 그 401을
가렸다 — **첫 오류만 보고 원인을 단정하면 안 된다**.
"""
import asyncio
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.integrations.egress import check_host
from erp.audit.service import AuditService
from erp.config import get_settings
from erp.hr.models import Employee, MedicalStaffContract

logger = logging.getLogger("erp.integrations.esign_emp_map")
_audit = AuditService()


def _read_key() -> str | None:
    """디렉터리 조회용 — PII 읽기 키 우선."""
    s = get_settings()
    return s.his_pii_read_key or s.his_webhook_key


def _write_key() -> str | None:
    """map 쓰기용 — 웹훅 키. PII 읽기 키로 쓰면 401."""
    s = get_settings()
    return s.his_webhook_key


def _base() -> str:
    s = get_settings()
    base = s.his_api_base.rstrip("/")
    check_host("his", base, loopback=True)
    return base


async def _fetch_directory(key: str) -> list[dict]:
    """HIS 직원 디렉터리 — employeeNumber+clinicUserId 봉투 수용."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{_base()}/api/v1/integration/hr/staff",
                             headers={"X-Integration-Key": key})
        r.raise_for_status()
        body = r.json()
    items = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(items, dict):
        items = items.get("items", items)
    return items if isinstance(items, list) else []


#: 대량 백필의 상대 스로틀 대응 — 78건을 연속 POST했다가 **전건 429로 실패**했다.
#
# ⚠ 고정 간격으로는 부족했다. 0.35초로 늦춰도 62건 성공 후 나머지가 429로 떨어졌다 —
#   상대의 한도(분당 N건 등)를 우리가 모르기 때문이다. 그래서 **적응형**으로 만든다:
#   429를 만나면 그 요청만 재시도하는 게 아니라 **이후 전체 호출 간격을 늘린다**.
#   한 번 늘린 속도는 그 실행 안에서 되돌리지 않는다(다시 부딪히는 것보다 느린 게 낫다).
_PACE_START_S = 0.35
_PACE_MAX_S = 5.0
_RETRY_STATUS = (429, 500, 502, 503, 504)
_MAX_TRY = 6


class _Pace:
    """실행 단위 호출 간격 — 429를 만날 때마다 늘어난다."""

    def __init__(self) -> None:
        self.s = _PACE_START_S

    def slow_down(self) -> None:
        self.s = min(self.s * 2, _PACE_MAX_S)

    async def wait(self) -> None:
        await asyncio.sleep(self.s)


async def _post_map(emp_no: str, erp_code: str, key: str, pace: _Pace) -> None:
    """사번 map 1건 — 429/5xx는 백오프 재시도, 그 외 4xx는 즉시 실패(건별 격리)."""
    delay = 1.0
    for attempt in range(1, _MAX_TRY + 1):
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{_base()}/api/v1/integration/hr/staff/{emp_no}/map",
                json={"erpEmployeeCode": erp_code},
                headers={"X-Integration-Key": key})
        if r.status_code < 400:
            return
        if r.status_code not in _RETRY_STATUS or attempt == _MAX_TRY:
            r.raise_for_status()
        if r.status_code == 429:
            pace.slow_down()          # 이후 전체 호출도 늦춘다
        # Retry-After를 주면 그것을 우선한다(상대가 아는 대기시간이 더 정확하다)
        wait = float(r.headers.get("Retry-After") or 0) or delay
        await asyncio.sleep(min(wait, 30.0))
        delay *= 2


async def backfill_external_ids(session: AsyncSession, *, actor: int | None = None,
                                dry_run: bool = True, limit: int | None = None) -> dict:
    """HIS → ERP 방향(B-2) — ERP `external_user_id`가 빈 직원을 디렉터리로 채운다.

    `backfill()`(ERP→HIS)은 external_user_id가 **이미 있는** 직원만 다룰 수 있어, 그것이 없는
    직원은 영영 연결되지 않았다. 디렉터리 EP가 `employeeNumber`와 `clinicUserId`를 **둘 다**
    주므로 사번으로 조인하면 반대 방향을 채울 수 있다 — HIS에 추가 작업도, 쓰기도 필요 없다.

    안전 규칙:
      · 이미 값이 있는 직원은 **건드리지 않는다**(덮어쓰기 금지).
      · 한 clinicUserId가 여러 직원에 붙지 않도록 기존 사용분과 배치 내 중복을 모두 배제한다.
      · 기본은 `dry_run=True` — 무엇이 바뀌는지 먼저 보고 적용한다(HIS 제안 "1명 시험 → 배치").
    """
    key = _read_key()      # 역방향은 조회만 — 쓰기 키 불요
    if not key:
        return {"skipped": "연동키 미설정(HIS_PII_READ_KEY)"}
    directory = await _fetch_directory(key)
    by_empno: dict[str, str] = {}
    for row in directory:
        cu = row.get("clinicUserId") or row.get("clinic_user_id")
        en = row.get("employeeNumber") or row.get("employee_number")
        if cu and en:
            by_empno[str(en)] = str(cu)

    taken = set((await session.scalars(select(Employee.external_user_id).where(
        Employee.external_user_id.isnot(None)))).all())
    emps = (await session.scalars(select(Employee).where(
        Employee.resigned_on.is_(None), Employee.external_user_id.is_(None),
        Employee.employee_no.isnot(None)).order_by(Employee.id))).all()

    planned, skipped_dup, no_dir = [], 0, 0
    for e in emps:
        cu = by_empno.get(str(e.employee_no))
        if cu is None:
            no_dir += 1
            continue
        if cu in taken:                      # 이미 다른 직원에 붙은 id — 1:1 위반 방지
            skipped_dup += 1
            continue
        taken.add(cu)
        planned.append((e, cu))
        if limit and len(planned) >= limit:
            break

    if not dry_run:
        for e, cu in planned:
            e.external_user_id = cu
        await session.flush()

    out = {"directory_rows": len(by_empno), "candidates": len(emps),
           "planned": len(planned), "applied": 0 if dry_run else len(planned),
           "skipped_duplicate_clinic_id": skipped_dup,
           "unmatched_no_directory": no_dir, "dry_run": dry_run,
           "sample": [e.employee_no for e, _ in planned[:5]]}
    if not dry_run:
        await _audit.record_audit(session, actor=actor, action="SYNC",
                                  entity="integrations.esign_emp_map.external_ids", after=out)
    return out


async def backfill(session: AsyncSession, *, actor: int | None = None,
                   signatories_only: bool = True, dry_run: bool = False) -> dict:
    """HIS erpEmployeeCode 백필. signatories_only=의료계약 서명자 우선. dry_run=조인만·발신 없음."""
    read_key, write_key = _read_key(), _write_key()
    if not read_key or not write_key:
        return {"skipped": "연동키 미설정(조회=HIS_PII_READ_KEY·쓰기=HIS_WEBHOOK_KEY)"}
    directory = await _fetch_directory(read_key)
    # 디렉터리 clinicUserId → employeeNumber
    by_clinic: dict[str, str] = {}
    for row in directory:
        cu = row.get("clinicUserId") or row.get("clinic_user_id")
        en = row.get("employeeNumber") or row.get("employee_number")
        if cu and en:
            by_clinic[str(cu)] = str(en)

    # 대상 ERP 직원 — 서명자 우선(ACTIVE 의료계약) 또는 재직 전원
    q = select(Employee).where(Employee.resigned_on.is_(None),
                               Employee.external_user_id.isnot(None))
    if signatories_only:
        sig_ids = set((await session.scalars(select(MedicalStaffContract.employee_id).where(
            MedicalStaffContract.status == "ACTIVE"))).all())
        if not sig_ids:
            return {"target": "signatories", "matched": 0, "mapped": 0,
                    "note": "ACTIVE 의료계약 서명자 없음"}
        q = q.where(Employee.id.in_(sig_ids))
    emps = (await session.scalars(q)).all()

    matched, mapped, failed, no_dir = [], 0, [], 0
    pace = _Pace()
    for e in emps:
        emp_no = by_clinic.get(str(e.external_user_id))
        if emp_no is None:
            no_dir += 1
            continue
        matched.append({"employee_no": e.employee_no, "his_employee_number": emp_no})
        if dry_run:
            continue
        try:
            await _post_map(emp_no, e.employee_no, write_key, pace)
            mapped += 1
        except Exception as exc:  # noqa: BLE001 — 건별 격리
            failed.append({"employee_no": e.employee_no, "err": str(exc)[:120]})
            logger.warning("사번 map 실패", extra={"ctx": {"emp": e.employee_no,
                                                        "err": str(exc)[:120]}})
        await pace.wait()                # 상대 스로틀 회피 — 429를 만나면 자동으로 느려진다
    out = {"target": "signatories" if signatories_only else "all",
           "directory_rows": len(by_clinic), "candidates": len(emps),
           "matched": len(matched), "mapped": mapped, "failed": len(failed),
           "unmatched_no_directory": no_dir, "dry_run": dry_run,
           "final_pace_s": round(pace.s, 2)}
    if not dry_run:
        await _audit.record_audit(session, actor=actor, action="SYNC",
                                  entity="integrations.esign_emp_map", after=out)
    return out
