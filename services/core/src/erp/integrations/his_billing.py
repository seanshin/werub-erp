"""HIS 진료라인 백필 pull (D-6 회신서 §1-④ — API pull 채택).

운영 전환 시 재원/기간 내 기존 발생분 일괄 적재: HIS `GET /api/v1/integration/billing/lines`
→ 건별 _billing_line 재사용(멱등 치환 — 이벤트 수신분과 충돌 없음).
인증: X-Integration-Key — billing/lines(환자 PII)는 PII 전용키(his_pii_read_key, 미설정 시
webhook_key 폴백·무중단, S-1 계약 his-erp-integration-auth-hardening). SSRF: his_api_base 호스트 고정.
"""
import logging
from datetime import date

import httpx

from erp.integrations.egress import check_host

from erp.config import get_settings
from erp.integrations.inbound_consumer import _billing_line

logger = logging.getLogger("erp.integrations.his_billing")


async def backfill_lines(session, *, from_on: date, to_on: date) -> dict:
    s = get_settings()
    # billing/lines는 환자 청구 PII → PII 전용키(S-1). 미설정 시 webhook_key 폴백(무중단).
    pii_key = s.his_pii_read_key or s.his_webhook_key
    if not pii_key:
        return {"pulled": 0, "applied": 0, "skipped": "연동키 미설정"}
    base = s.his_api_base.rstrip("/")
    check_host("his", base, loopback=True)

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{base}/api/v1/integration/billing/lines",
                             params={"from": from_on.isoformat(), "to": to_on.isoformat()},
                             headers={"X-Integration-Key": pii_key})
        r.raise_for_status()
        body = r.json()
    # 응답 봉투 변형 수용: [..] | {items:[..]} | {data:[..]} | {data:{items:[..]}}
    items = body
    if isinstance(items, dict):
        items = items.get("data", items)
    if isinstance(items, dict):
        items = items.get("items")
    items = items if isinstance(items, list) else []

    applied = failed = 0
    reasons: dict[str, int] = {}
    for it in items:
        try:
            async with session.begin_nested():     # 건별 savepoint — 세션 오염 방지
                await _billing_line(session, it, "billing.line_recorded", {})
            applied += 1
        except Exception as exc:  # noqa: BLE001 — 건별 격리
            failed += 1
            key = str(exc)[:80]
            reasons[key] = reasons.get(key, 0) + 1
            logger.warning("백필 라인 실패", extra={"ctx": {"error": key}})
    out = {"pulled": len(items), "applied": applied, "failed": failed,
           "fail_reasons": dict(sorted(reasons.items(), key=lambda x: -x[1])[:5])}
    # 경계 통과 감사(Phase0 G3) — 환자 청구 PII pull의 불변 트레일
    from erp.audit.service import AuditService
    await AuditService().record_audit(session, actor=None, action="SYNC",
                                      entity="integrations.his_billing_backfill",
                                      after={"from": from_on.isoformat(),
                                             "to": to_on.isoformat(), **out})
    return out


async def _pull(path: str, params: dict | None = None) -> list[dict]:
    """HIS integration EP 공용 pull — {data:[..]} 봉투, SSRF·키 가드."""
    s = get_settings()
    if not s.his_webhook_key:
        return []
    base = s.his_api_base.rstrip("/")
    check_host("his", base, loopback=True)
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{base}{path}", params=params or {},
                             headers={"X-Integration-Key": s.his_webhook_key})
        r.raise_for_status()
        body = r.json()
    items = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(items, dict):          # 중첩 봉투 {data:{items:[..]}} 수용
        items = items.get("items", items)
    return items if isinstance(items, list) else []


async def pull_unbilled_progress(session, *, as_of: date) -> dict:
    """HIS 미청구 진행분 pull → 미청구수익(계약자산) 발생 (K-IFRS 1115 B-1).

    HIS 응답(camelCase, {data:{items:[..]}}): progressBasis ACTUAL_LINES(입원 FFS)·SAME_DAY(외래)=
    incurredAmount 직접, LOS_RATIO(정액)=drgAmount×daysAdmitted/standardDays(임상경로 연결 시).
    미청구 = unbilledAmount(HIS 산출) 우선, 없으면 max(0, incurred−billed). status∈(IN_PROGRESS, DISCHARGED_UNBILLED).
    """
    items = await _pull("/api/v1/integration/billing/unbilled-progress",
                        {"as_of": as_of.isoformat()})
    total = 0.0
    by_dept: dict[str, float] = {}
    counted = skipped = 0
    for it in items:
        if it.get("status") not in ("IN_PROGRESS", "DISCHARGED_UNBILLED"):
            skipped += 1
            continue
        if it.get("unbilledAmount") is not None:        # HIS 산출 우선
            unbilled = float(it["unbilledAmount"])
        else:
            billed = float(it.get("billedAmount") or 0)
            incurred = it.get("incurredAmount")
            if incurred is None and it.get("progressBasis") == "LOS_RATIO":
                drg = float(it.get("drgAmount") or 0)
                days = float(it.get("daysAdmitted") or 0)
                std = float(it.get("standardDays") or 0)
                incurred = drg * min(days / std, 1.0) if std > 0 else 0.0
            unbilled = max(0.0, float(incurred or 0) - billed)
        if unbilled <= 0:
            skipped += 1
            continue
        total += unbilled
        dc = it.get("deptCode") or "-"
        by_dept[dc] = by_dept.get(dc, 0.0) + unbilled
        counted += 1
    yyyymm = as_of.strftime("%Y-%m")
    accrual = None
    if round(total) > 0:
        from erp.finance.revenue_recognition_service import RevenueRecognitionService
        accrual = await RevenueRecognitionService().accrue_unbilled(
            session, yyyymm=yyyymm, amount=round(total))
    return {"as_of": as_of.isoformat(), "yyyymm": yyyymm, "rows": len(items),
            "counted": counted, "skipped": skipped, "unbilled_total": round(total),
            "by_dept": {k: round(v) for k, v in by_dept.items()}, "accrual": accrual}


async def sync_noncovered(session) -> dict:
    """HIS 비급여 가격표(NonCoveredItem) → fee_master NONCOVERED 기관가 적재.

    고시 목록(362건, 가격 0)과 별개 축 — HIS 기관 코드(CT-/EX- 등)·실가격이 SoT.
    (code, NONCOVERED, valid_from) 멱등. 버전은 **올해 1월 1일**(KST) — 가격·명칭이 바뀌면 올해 버전만 갱신하고
    지난해 버전은 그대로 둔다(2026-10-10: valid_from이 2026-01-01 고정이라 2027 가격 변경이 2026 이력을 덮어쓸 뻔했다).
    직전 버전과 같으면 새 버전을 만들지 않는다(조회는 valid_from ≤ 기준일 최신행).
    """
    from datetime import date as _date

    from sqlalchemy import select

    from erp.regulatory.models import FeeMaster

    from erp.common.kst import kst_today

    items = await _pull("/api/v1/integration/billing/noncovered")
    valid_from = _date(kst_today().year, 1, 1)
    created = updated = 0
    for it in items:
        code, name = it.get("code"), it.get("name")
        price = float(it.get("price") or 0)
        if not code:
            continue
        row = (await session.execute(select(FeeMaster).where(
            FeeMaster.code == code, FeeMaster.code_type == "NONCOVERED",
            FeeMaster.valid_from == valid_from))).scalar_one_or_none()
        if row is None:
            prev = await session.scalar(select(FeeMaster).where(
                FeeMaster.code == code, FeeMaster.code_type == "NONCOVERED",
                FeeMaster.valid_from < valid_from).order_by(FeeMaster.valid_from.desc()).limit(1))
            if prev is not None and float(prev.unit_price) == price and (not name or prev.name == name):
                continue                       # 직전 버전과 같음 — 새 버전 불필요
            if prev is not None and (prev.valid_to is None or prev.valid_to > valid_from):
                prev.valid_to = valid_from     # 옛 버전을 닫고 새 버전을 연다(겹치는 열린 버전 금지 — regulatory/sync 관례)
            session.add(FeeMaster(code=code, code_type="NONCOVERED",
                                  name=name or code, unit_price=price,
                                  valid_from=valid_from))
            created += 1
        elif float(row.unit_price) != price or (name and row.name != name):
            row.unit_price, row.name = price, name or row.name
            updated += 1
    return {"pulled": len(items), "created": created, "updated": updated}


async def sync_materials(session) -> dict:
    """HIS InventoryItem → scm.items 무매핑 등록 (code=HIS 코드 그대로, 합의 2026-06-12)."""
    from sqlalchemy import select

    from erp.scm.models import Item

    items = await _pull("/api/v1/integration/billing/materials")
    created = updated = skipped = 0
    for it in items:
        code, name = it.get("code"), it.get("name")
        if not code or len(str(code)) > 20:
            skipped += 1
            continue
        row = (await session.execute(select(Item).where(Item.code == code))
               ).scalar_one_or_none()
        # HIS 분류(한글) → ERP CHECK(DRUG/SUPPLY/EQUIPMENT) 매핑
        raw_cat = str(it.get("category") or "")
        if any(k in raw_cat for k in ("약", "주사제", "DRUG")):
            category = "DRUG"
        elif any(k in raw_cat for k in ("장비", "기기", "EQUIP")):
            category = "EQUIPMENT"
        else:
            category = "SUPPLY"
        unit = (it.get("unit") or "EA")[:10]
        if row is None:
            session.add(Item(code=code, name=name or code, category=category, unit=unit))
            created += 1
        elif name and row.name != name:
            row.name = name
            updated += 1
    return {"pulled": len(items), "created": created, "updated": updated,
            "skipped": skipped}


async def fetch_census(from_on: date, to_on: date) -> dict:
    """HIS 0시 재원 census — 간호등급 정식 재원일수. {patient_days, days:[..]}."""
    items = await _pull("/api/v1/integration/billing/census",
                        {"from": from_on.isoformat(), "to": to_on.isoformat()})
    total = 0.0
    for it in items:
        # HIS 응답 키: inpatientCensus (0시 재원 환자수) — patientCount/count 관용 수용
        total += float(it.get("inpatientCensus") or it.get("patientCount")
                       or it.get("count") or 0)
    return {"days": len(items), "patient_days": total}
