"""ERP↔HIS 수가 코드 마스터 교환 API (미등재 코드 상호 보강).

- ERP 코드 마스터 제공(HIS pull): 진료/청구 시 단가·시행일·분류·산정기준 참조.
- 미등재 코드 리포트: ERP가 수신한 진료 라인 중 마스터 미등재 코드(HIS가 정보 제공할 대상).
인증: X-Integration-Key(his_webhook_key 공유). 접속기록(서버간 — 시스템 주체).
"""
import hmac
from datetime import date

from fastapi import APIRouter, Depends, Header
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.config import get_settings
from erp.db import get_session
from erp.errors import ForbiddenError
from erp.regulatory.models import FeeMaster

router = APIRouter(prefix="/integration/regulatory", tags=["integrations"])
_audit = AuditService()
_SYSTEM_ACTOR = 0


def _auth(key: str | None) -> None:
    s = get_settings()
    if not s.his_webhook_key:
        raise ForbiddenError("연동 게이트웨이 미설정")
    if not key or not hmac.compare_digest(key, s.his_webhook_key):
        raise ForbiddenError("연동 인증 실패")


@router.get("/fees")
async def get_fees(codes: str | None = None, code_type: str | None = None,
                   as_of: date | None = None, limit: int = 500,
                   x_integration_key: str | None = Header(default=None),
                   session: AsyncSession = Depends(get_session)):
    """ERP 코드 마스터 조회 (HIS 참조) — 진료일(as_of) 시점 유효분.

    codes(쉼표, 최대 500) 또는 code_type 필터. 단가·시행일·유효기간·분류·산정기준 반환.
    """
    _auth(x_integration_key)
    on = as_of or date.today()
    q = (select(FeeMaster).where(FeeMaster.valid_from <= on,
         or_(FeeMaster.valid_to.is_(None), FeeMaster.valid_to > on))
         .limit(min(limit, 500)))
    if codes:
        codelist = [c.strip() for c in codes.split(",") if c.strip()][:500]
        q = q.where(FeeMaster.code.in_(codelist))
    if code_type:
        q = q.where(FeeMaster.code_type == code_type)
    rows = (await session.scalars(q)).all()
    await _audit.record_access(session, actor=_SYSTEM_ACTOR, action="VIEW",
                               entity="regulatory.fee_master(HIS)",
                               route="/integration/regulatory/fees")
    return {"as_of": on, "count": len(rows), "items": [
        {"code": r.code, "code_type": r.code_type, "name": r.name,
         "unit_price": float(r.unit_price), "benefit_category": r.benefit_category,
         "valid_from": r.valid_from, "valid_to": r.valid_to,
         "criteria_text": r.criteria_text, "source": r.source} for r in rows]}


@router.get("/unmapped-codes")
async def unmapped_codes(from_on: date | None = None, to_on: date | None = None,
                         x_integration_key: str | None = Header(default=None),
                         session: AsyncSession = Depends(get_session)):
    """ERP 미등재 코드 리포트 — 진료 라인 중 fee_master 현행 미등재 코드.

    HIS가 이 목록의 코드 정보(단가·명칭·시행일·산정기준)를 제공하면 ERP 마스터 보강.
    HIS unitPrice 폴백으로 진료비는 계산되나 ERP 마스터 SoT 미확립 상태.
    """
    _auth(x_integration_key)
    sql = text("""
        SELECT bl.code, bl.code_type, count(*) cnt,
               max(bl.unit_price_his) his_price, min(bl.treated_on) first_on
        FROM patient.billing_lines bl
        LEFT JOIN regulatory.fee_master fm
          ON fm.code = bl.code AND fm.valid_to IS NULL
        WHERE bl.cancelled = false AND fm.code IS NULL
          AND (CAST(:f AS date) IS NULL OR bl.treated_on >= CAST(:f AS date))
          AND (CAST(:t AS date) IS NULL OR bl.treated_on <= CAST(:t AS date))
        GROUP BY bl.code, bl.code_type
        ORDER BY cnt DESC
    """)
    rows = (await session.execute(sql, {"f": from_on, "t": to_on})).all()
    await _audit.record_access(session, actor=_SYSTEM_ACTOR, action="VIEW",
                               entity="regulatory.unmapped_codes(HIS)",
                               route="/integration/regulatory/unmapped-codes")
    return {"count": len(rows), "items": [
        {"code": c, "code_type": ct, "occurrences": cnt,
         "his_unit_price": float(hp) if hp is not None else None,
         "first_seen_on": fo} for c, ct, cnt, hp, fo in rows]}


@router.get("/drug-map/confirmed")
async def confirmed_drug_map(limit: int = 2000,
                             x_integration_key: str | None = Header(default=None),
                             session: AsyncSession = Depends(get_session)):
    """확정된 약품 코드 매핑 (Close-loop) — HIS가 pull → insuranceCode 백필.

    ERP 약제팀이 확정한 his_code↔보험코드를 HIS가 받아 채우면, 이후 HIS가 표준
    보험코드로 청구 → ERP 확정 단계 없이 즉시 price_source=MASTER. 두 채널 수렴.
    """
    _auth(x_integration_key)
    from erp.regulatory.models import DrugCodeMap
    rows = (await session.scalars(select(DrugCodeMap).where(
        DrugCodeMap.status == "CONFIRMED",
        DrugCodeMap.insurance_code.isnot(None))
        .order_by(DrugCodeMap.his_code).limit(min(limit, 5000)))).all()
    await _audit.record_access(session, actor=_SYSTEM_ACTOR, action="VIEW",
                               entity="regulatory.drug_code_map(HIS)",
                               route="/integration/regulatory/drug-map/confirmed")
    return {"count": len(rows), "items": [
        {"his_code": m.his_code, "insurance_code": m.insurance_code,
         "drug_name": m.drug_name, "matched_name": m.matched_name} for m in rows]}
