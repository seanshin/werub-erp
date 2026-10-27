"""Phase0 가명 토큰(patientRef) 대사·역해소 (HIS 통지 2026-07-24).

HIS가 `pseudonymizeChartNo` 플립 후 외부 이벤트의 chartNo 필드에 patientRef를 싣는다:
    patientRef = HMAC-SHA256(chartNo, key) hex 앞 16자   (결정적 — 환자당 안정)

ERP는 1회성 대사표(chartNo,patientRef 873명)를 integration.patient_ref_map에 이관하고,
컷오버(his_pseudonym_enabled ON) 후 수신 patientRef를 원 chart_no로 역해소한다.
대사표에 없는(신규 환자) patientRef는 DEAD 처리 대신 보류 큐(pending_patient_ref)에 적재.

키는 config.pseudonym_key(파일 0600 우선). 미설정 시 이관·역해소 불가(fail-safe: 원본 미노출).
"""
import hashlib
import hmac
import logging

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from erp.config import get_settings
from erp.integrations.models import PatientRefMap, PendingPatientRef

logger = logging.getLogger("erp.integrations.patient_ref")


class PendingRefError(Exception):
    """미등록 patientRef 수신 — 대사표 역해소 실패(신규 환자). 보류 큐 적재는 consumer 외곽에서
    (핸들러 savepoint 롤백에 pending insert가 휩쓸리지 않도록). 재시도 시 증분 대사표로 자기치유."""

    def __init__(self, patient_ref: str, event_type: str | None = None):
        self.patient_ref = str(patient_ref)
        self.event_type = event_type
        super().__init__(f"미등록 patientRef 보류: {event_type}")


def token_for(chart_no: str, key: str) -> str:
    """patientRef = HMAC-SHA256(chartNo, key) hex 앞 16자 (HIS 규격 일치)."""
    mac = hmac.new(key.encode(), str(chart_no).encode(), hashlib.sha256)
    return mac.hexdigest()[:16]


def key_fingerprint(key: str) -> str:
    """키 지문 = md5(key) 앞 12자 — HIS 전달값(`f85cc2502a20`)과 상호검증(키 원문 비노출)."""
    return hashlib.md5(key.encode()).hexdigest()[:12]  # noqa: S324 — 지문 확인용(비암호)


async def ingest_map(session: AsyncSession, rows: list[tuple[str, str]], *,
                     key: str | None = None, verify: bool = True) -> dict:
    """대사표 이관 — rows=[(chart_no, patient_ref)]. 멱등 upsert(기존은 patient_ref 갱신).

    verify=True·key 제공 시 각 행의 patient_ref가 token_for(chart_no,key)와 일치하는지 재계산
    검증(키·표 정합 확증). 불일치 행은 이관에서 제외하고 mismatched로 보고(원본 오염 차단).
    """
    ingested = updated = mismatched = 0
    mismatch_samples: list[str] = []
    existing = {r.chart_no: r for r in (await session.scalars(select(PatientRefMap))).all()}
    seen_refs: set[str] = set()
    dup_ref = 0
    for chart_no, patient_ref in rows:
        chart_no, patient_ref = str(chart_no).strip(), str(patient_ref).strip()
        if not chart_no or not patient_ref:
            continue
        if verify and key is not None and token_for(chart_no, key) != patient_ref:
            mismatched += 1
            if len(mismatch_samples) < 5:
                mismatch_samples.append(chart_no)
            continue
        if patient_ref in seen_refs:   # 표 내부 중복 patientRef(충돌) — 1:1 위반, 건너뜀
            dup_ref += 1
            continue
        seen_refs.add(patient_ref)
        row = existing.get(chart_no)
        if row is None:
            session.add(PatientRefMap(chart_no=chart_no, patient_ref=patient_ref, source="HIS_MAP"))
            ingested += 1
        elif row.patient_ref != patient_ref:
            row.patient_ref = patient_ref
            updated += 1
    await session.flush()
    total = int((await session.scalar(select(func.count()).select_from(PatientRefMap))) or 0)
    out = {"rows_in": len(rows), "ingested": ingested, "updated": updated,
           "mismatched": mismatched, "duplicate_ref": dup_ref, "total_mapped": total,
           "verified": bool(verify and key is not None)}
    if mismatch_samples:
        out["mismatch_chart_samples"] = mismatch_samples
    return out


async def resolve(session: AsyncSession, patient_ref: str) -> str | None:
    """patientRef → 원 chart_no. 미등록 시 None."""
    return await session.scalar(
        select(PatientRefMap.chart_no).where(PatientRefMap.patient_ref == str(patient_ref)))


async def record_pending(session: AsyncSession, patient_ref: str, *,
                         event_type: str | None = None) -> None:
    """미등록 patientRef 보류 적재 — 동일 토큰 재수신 시 count만 증가(멱등)."""
    ref = str(patient_ref)
    n = await session.execute(
        update(PendingPatientRef)
        .where(PendingPatientRef.patient_ref == ref,
               PendingPatientRef.resolved_chart_no.is_(None))
        .values(event_count=PendingPatientRef.event_count + 1))
    if n.rowcount == 0:
        session.add(PendingPatientRef(patient_ref=ref, first_event=event_type, event_count=1))
    await session.flush()


async def cutover_enabled(session: AsyncSession) -> bool:
    """컷오버 게이트 판정 — env HIS_PSEUDONYM_ENABLED 또는 런타임 DB 플래그(관리화면 토글) OR.

    env=fail-safe 상시 경로(레거시)·DB=관리화면 무재시작 토글. 둘 중 하나라도 ON이면 활성.
    """
    if get_settings().his_pseudonym_enabled:
        return True
    from erp.common.org_service import OrgSettingsService
    return await OrgSettingsService().get_phase0_cutover(session)


async def resolve_inbound_chart(session: AsyncSession, raw: str, *,
                                event_type: str | None = None) -> str | None:
    """수신 chartNo 필드 역해소 진입점 (inbound_consumer용).

    컷오버 OFF: 원 chartNo 그대로 반환(무변경). 컷오버 ON(env 또는 DB 플래그): raw=patientRef로
    간주해 대사표 역해소. 미등록이면 PendingRefError(consumer 외곽에서 보류 큐 적재·재시도).
    키가 없어도 대사표만으로 역해소(키는 이관·검증 단계에서만 필요).
    """
    if not await cutover_enabled(session):
        return raw
    chart_no = await resolve(session, raw)
    if chart_no is None:
        raise PendingRefError(raw, event_type)
    return chart_no
