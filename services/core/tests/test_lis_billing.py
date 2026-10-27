"""LIS 검사 청구 캡처 (E-3 A·산정 ERP) — 매핑·산정·미매핑 보류·revision 정정·취소·consumer."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.integrations.inbound_consumer import InboundConsumer
from erp.integrations.lis_billing_service import LisBillingService
from erp.integrations.models import HisInboundEvent, LisBillingClaim, LisTestEdiMap
from erp.regulatory.models import FeeMaster

svc = LisBillingService()
consumer = InboundConsumer()


async def _map(session, test_code, edi):
    session.add(LisTestEdiMap(test_code=test_code, edi_code=edi, name=test_code, active=1))
    await session.flush()


async def _fee(session, code, price):
    session.add(FeeMaster(code=code, code_type="TARIFF", name=code, unit_price=price,
                          valid_from=date(2026, 1, 1)))
    await session.flush()


def _payload(lis_id="LIS-O-1", rev=1, code="L2401", qty=1, status="FINAL"):
    return {"lisBillingId": lis_id, "revision": rev, "patientId": "P202600123",
            "performedOn": "2026-07-12",
            "items": [{"testCode": code, "testName": "CBC", "quantity": qty,
                       "specimen": "WB", "resultStatus": status}]}


async def test_capture_mapped_prices(session):
    await _map(session, "L2401", "AA100")
    await _fee(session, "AA100", 5000)
    out = await svc.capture(session, _payload(qty=2))
    assert out["status"] == "CAPTURED" and out["mapped"] is True and out["total_amount"] == 10000
    assert out["chart_no"] == "P202600123"      # 게이트 OFF → passthrough


async def test_capture_unmapped_pending_amount(session):
    out = await svc.capture(session, _payload(code="UNKNOWN"))
    assert out["status"] == "CAPTURED" and out["mapped"] is False and out["total_amount"] is None


async def test_mapped_but_no_fee_is_pending(session):
    await _map(session, "L9", "ZZ999")           # 매핑은 있으나 수가 마스터 없음
    out = await svc.capture(session, _payload(code="L9"))
    assert out["mapped"] is False and out["total_amount"] is None   # 보류(캡처는 됨)


async def test_revision_replaces(session):
    await _map(session, "L2401", "AA100")
    await _fee(session, "AA100", 5000)
    await svc.capture(session, _payload(rev=1, qty=1))
    out = await svc.capture(session, _payload(rev=2, qty=3))       # 정정 — 대체
    assert out["revision"] == 2 and out["total_amount"] == 15000
    claims = (await session.scalars(select(LisBillingClaim).where(
        LisBillingClaim.lis_billing_id == "LIS-O-1"))).all()
    assert len(claims) == 1 and claims[0].revision == 2 and claims[0].item_count == 1


async def test_stale_revision_ignored(session):
    await svc.capture(session, _payload(rev=3))
    out = await svc.capture(session, _payload(rev=2))             # 과거 리비전 무시
    assert out.get("stale_ignored") is True and out["revision"] == 3


async def test_cancelled_all_items(session):
    out = await svc.capture(session, _payload(status="CANCELLED"))
    assert out["status"] == "CANCELLED" and out["total_amount"] is None


async def test_mixed_order_skips_cancelled_item(session):
    await _map(session, "L2401", "AA100")
    await _fee(session, "AA100", 5000)
    # 한 오더에 FINAL(산정) + CANCELLED(제외) — 취소 항목은 산정·집계 제외
    payload = {"lisBillingId": "LIS-MIX", "revision": 1, "patientId": "P1",
               "performedOn": "2026-07-12",
               "items": [{"testCode": "L2401", "quantity": 1, "resultStatus": "FINAL"},
                         {"testCode": "L2401", "quantity": 5, "resultStatus": "CANCELLED"}]}
    out = await svc.capture(session, payload)
    assert out["status"] == "CAPTURED" and out["mapped"] is True
    assert out["total_amount"] == 5000            # 취소 항목(×5) 제외, FINAL 1건만


async def test_required_fields(session):
    with pytest.raises(ValidationError):
        await svc.capture(session, {"revision": 1, "items": []})   # lisBillingId·items 누락


async def test_status_polling(session):
    await _map(session, "L2401", "AA100")
    await _fee(session, "AA100", 5000)
    await svc.capture(session, _payload())
    st = await svc.status(session, lis_billing_id="LIS-O-1")
    assert st["status"] == "ACCEPTED" and st["mapped"] is True
    # Phase0 egress — 폴링 응답에 원 chart_no(MRN) 미포함(LIS 역유출 차단)
    assert "chart_no" not in st
    assert await svc.status(session, lis_billing_id="NOPE") is None


async def test_consumer_end_to_end(session):
    await _map(session, "L2401", "AA100")
    await _fee(session, "AA100", 5000)
    session.add(HisInboundEvent(domain="lis", event_type="lis.billing",
                                source_ref="lis:LIS-O-9:1", status="NEW", payload=_payload("LIS-O-9")))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] >= 1
    ev = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == "lis:LIS-O-9:1"))
    assert ev.status == "APPLIED"
    claim = await session.scalar(select(LisBillingClaim).where(
        LisBillingClaim.lis_billing_id == "LIS-O-9"))
    assert claim is not None and float(claim.total_amount) == 5000


# ── 관리 EP — 파일럿·오전송 정리(목록·취소) ──────────────────────────────────

async def test_admin_cancel_and_list(session):
    from erp.common.router import CurrentUser
    from erp.errors import ForbiddenError
    from erp.integrations.lis_webhook_router import (
        cancel_lis_billing_claim, list_lis_billing_claims,
    )
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    staff = CurrentUser(user_id=2, role="STAFF", dept_id=1, license_flag=True)

    await svc.capture(session, _payload("LIS-PILOT-1"))
    out = await cancel_lis_billing_claim("LIS-PILOT-1", user=admin, session=session)
    assert out["status"] == "CANCELLED"
    claim = await session.scalar(select(LisBillingClaim).where(
        LisBillingClaim.lis_billing_id == "LIS-PILOT-1"))
    assert claim.status == "CANCELLED"
    # 폴링 응답에도 CANCELLED로 반영(LIS 확인 경로)
    st = await svc.status(session, lis_billing_id="LIS-PILOT-1")
    assert st["status"] == "CANCELLED"
    # 멱등 — 이미 취소면 already
    again = await cancel_lis_billing_claim("LIS-PILOT-1", user=admin, session=session)
    assert again.get("already") is True
    # 권한 — STAFF는 목록·취소 불가
    with pytest.raises(ForbiddenError):
        await cancel_lis_billing_claim("LIS-PILOT-1", user=staff, session=session)
    with pytest.raises(ForbiddenError):
        await list_lis_billing_claims(user=staff, session=session)
    # 존재하지 않는 청구
    with pytest.raises(ValidationError):
        await cancel_lis_billing_claim("NOPE", user=admin, session=session)
    # 목록 — status 필터·chart_no 마스킹(원문 미노출)
    lst = await list_lis_billing_claims(status="CANCELLED", user=admin, session=session)
    assert lst["count"] == 1 and lst["items"][0]["lis_billing_id"] == "LIS-PILOT-1"
    assert lst["items"][0]["chart_no"] != "P202600123"


async def test_cancel_then_revision_recaptures(session):
    """관리 취소 후 LIS 상위 revision 정정 재수신 — 정정이 우선(상태 재판정)."""
    from erp.common.router import CurrentUser
    from erp.integrations.lis_webhook_router import cancel_lis_billing_claim
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    await svc.capture(session, _payload("LIS-RC-1"))
    await cancel_lis_billing_claim("LIS-RC-1", user=admin, session=session)
    out = await svc.capture(session, _payload("LIS-RC-1", rev=2))
    assert out["status"] == "CAPTURED"
