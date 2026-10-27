"""LIS 검사 청구 캡처·산정 (E-3 A: LIS 수량·ERP 산정) — 오더 단위 멱등·revision 정정.

흐름: 인박스 소비 → 식별자 역해소(Phase0 대응) → testCode→EDI 매핑(lis_test_edi_map) →
매핑된 항목은 fee lookup(as-of performed_on) 산정 → lis_billing_claims 캡처. 미매핑 검사는
캡처하되 amount 보류(mapped=0). 전 항목 CANCELLED면 청구 취소(CANCELLED).
"""
import logging
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import FeeNotFoundError, ValidationError
from erp.integrations.models import (
    LisBillingClaim, LisBillingClaimItem, LisTestEdiMap,
)
from erp.integrations.patient_ref import resolve_inbound_chart
from erp.regulatory.service import FeeLookupService

logger = logging.getLogger("erp.integrations.lis_billing")
_fee = FeeLookupService()


def _d(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


class LisBillingService:
    async def capture(self, session: AsyncSession, payload: dict, *,
                      event_type: str = "lis.billing") -> dict:
        lis_id = payload.get("lisBillingId") or payload.get("lis_billing_id")
        items = payload.get("items") or []
        if not lis_id:
            raise ValidationError("lisBillingId 필수")
        if not isinstance(items, list) or not items:
            raise ValidationError("items 필수")
        revision = int(payload.get("revision") or 1)
        performed_on = _d(payload.get("performedOn") or payload.get("performed_on"))
        # 식별자 역해소(Phase0 게이트 ON 시 patientRef→chart_no·미등록=PendingRefError로 보류)
        raw_pid = payload.get("patientId") or payload.get("patient_id")
        chart_no = await resolve_inbound_chart(session, str(raw_pid), event_type=event_type) \
            if raw_pid else None
        visit_id = payload.get("visitId") or payload.get("visit_id")

        existing = await session.scalar(select(LisBillingClaim).where(
            LisBillingClaim.lis_billing_id == str(lis_id)))
        if existing is not None and revision < existing.revision:
            return {"lis_billing_id": lis_id, "revision": existing.revision,
                    "status": existing.status, "stale_ignored": True}
        if existing is None:
            claim = LisBillingClaim(lis_billing_id=str(lis_id))
            session.add(claim)
            await session.flush()
        else:
            claim = existing
            await session.execute(delete(LisBillingClaimItem).where(
                LisBillingClaimItem.claim_id == claim.id))   # 정정 — 기존 항목 대체

        all_cancelled = all((it.get("resultStatus") or it.get("result_status"))
                            == "CANCELLED" for it in items)
        total = 0.0
        mapped_all = True
        for it in items:
            code = str(it.get("testCode") or it.get("test_code") or "").strip()
            qty = float(it.get("quantity") or 1)
            edi = await session.scalar(select(LisTestEdiMap.edi_code).where(
                LisTestEdiMap.test_code == code, LisTestEdiMap.active == 1))
            item_cancelled = (it.get("resultStatus") or it.get("result_status")) == "CANCELLED"
            unit_price = amount = None
            if item_cancelled:
                pass                          # 취소 항목은 산정·집계 제외(개별 취소)
            elif edi and performed_on:
                try:
                    fm = await _fee.get_price(session, code=edi, code_type="TARIFF",
                                              at_date=performed_on)
                    unit_price = float(fm.unit_price)
                    amount = round(unit_price * qty)
                    total += amount
                except FeeNotFoundError:
                    mapped_all = False        # 매핑은 됐으나 수가 마스터 미등재 → 보류
            elif not edi:
                mapped_all = False            # 코드 매핑 미등록 → 보류
            session.add(LisBillingClaimItem(
                claim_id=claim.id, test_code=code,
                test_name=it.get("testName") or it.get("test_name"), quantity=qty,
                specimen=it.get("specimen"),
                result_status=it.get("resultStatus") or it.get("result_status"),
                edi_code=edi, unit_price=unit_price, amount=amount))

        claim.revision = revision
        claim.chart_no = chart_no
        claim.visit_id = str(visit_id) if visit_id else None
        claim.performed_on = performed_on
        claim.item_count = len(items)
        claim.status = "CANCELLED" if all_cancelled else "CAPTURED"
        claim.mapped = 1 if (mapped_all and not all_cancelled) else 0
        claim.total_amount = round(total) if (mapped_all and not all_cancelled) else None
        await session.flush()
        return {"lis_billing_id": lis_id, "revision": revision, "status": claim.status,
                "chart_no": chart_no, "mapped": bool(claim.mapped),
                "total_amount": claim.total_amount, "items": len(items)}

    async def status(self, session: AsyncSession, *, lis_billing_id: str) -> dict | None:
        """폴링 — 캡처된 청구 상태. 미수신이면 None(라우터가 인박스 상태로 PENDING/REJECTED 판정)."""
        c = await session.scalar(select(LisBillingClaim).where(
            LisBillingClaim.lis_billing_id == str(lis_billing_id)))
        if c is None:
            return None
        # ⚠ chart_no(원 MRN) 미포함 — Phase0 컷오버 후 LIS는 patientRef만 알고 있어야 하며,
        # 폴링 응답에 원 chart_no를 실으면 egress로 MRN이 역유출됨. LIS는 lis_billing_id로 대사.
        return {"lis_billing_id": c.lis_billing_id, "revision": c.revision,
                "status": "ACCEPTED" if c.status == "CAPTURED" else "CANCELLED",
                "mapped": bool(c.mapped), "total_amount": float(c.total_amount)
                if c.total_amount is not None else None,
                "item_count": c.item_count}
