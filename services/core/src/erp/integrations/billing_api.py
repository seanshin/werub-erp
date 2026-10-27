"""HIS 수납 창구용 진료비 조회 API (D-6 요청서 §2) — 서버간, X-Integration-Key.

HIS 원무가 수납 시점에 호출 → ERP가 책정한 중간/최종 진료비 표시·수납 (B안 pull).
"""
import hmac

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.config import get_settings
from erp.db import get_session
from erp.errors import ForbiddenError
from erp.patient.billing_service import BillingService

router = APIRouter(prefix="/integration/billing", tags=["integrations"])
_svc = BillingService()
_audit = AuditService()
_SYSTEM_ACTOR = 0   # 서버간 호출(HIS 수납) — chartNo 열거 추적용 (H-1)


def _auth(x_integration_key: str | None) -> None:
    s = get_settings()
    if not s.his_webhook_key:
        raise ForbiddenError("연동 게이트웨이 미설정")
    if not x_integration_key or not hmac.compare_digest(x_integration_key, s.his_webhook_key):
        raise ForbiddenError("연동 인증 실패")


@router.get("/invoice")
async def billing_invoice(chartNo: str, encounterId: str | None = None,  # noqa: N803 — 요청서 계약 표기
                          interim: bool = True,
                          x_integration_key: str | None = Header(default=None),
                          session: AsyncSession = Depends(get_session)):
    """중간/최종 진료비 — 누적 라인×시점단가, 본인부담 분리, 기수납 차감, 사전심사 신호."""
    _auth(x_integration_key)
    await _audit.record_access(session, actor=_SYSTEM_ACTOR, action="VIEW",
                               entity="patient.billing_invoice", target_id=chartNo,
                               route="/integration/billing/invoice")
    return await _svc.invoice(session, chart_no=chartNo,
                              encounter_id=encounterId, interim=interim)
