"""스테이징 재처리(M-1)·FAILED 가시화(M-2) — admin 라우터 핸들러 직접 호출.

라우터는 서비스 계층 스타일로 검증(HTTP 미경유) — Depends 기본값은 명시 인자로 대체.
"""
from datetime import date

import pytest

from erp.app.admin_router import list_failed_staging, reprocess_staging
from erp.common.router import CurrentUser
from erp.errors import ValidationError
from erp.patient.models import HISPaymentStaging

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=1, license_flag=True)


async def _failed_payment(session, source_ref="PAY-2", reason="day_closed"):
    row = HISPaymentStaging(source_ref=source_ref, chart_no="C-001", amount=5000,
                            method="CASH", paid_on=date(2026, 5, 6), status="FAILED",
                            error_detail={"reason": reason, "chart_no": "C-001"})
    session.add(row)
    await session.flush()
    return row


# ── M-1: reprocess ──────────────────────────────────────────
async def test_reprocess_his_payment_failed_to_new(session):
    row = await _failed_payment(session)
    result = await reprocess_staging(row.id, kind="his_payment", user=ADMIN, session=session)
    assert result["status"] == "NEW"          # 소비 잡이 재소비 (FAILED→NEW)
    assert row.status == "NEW" and row.error_detail is None


async def test_reprocess_rejects_non_failed(session):
    row = await _failed_payment(session)
    row.status = "APPLIED"
    await session.flush()
    with pytest.raises(ValidationError, match="FAILED 상태만"):
        await reprocess_staging(row.id, kind="his_payment", user=ADMIN, session=session)


async def test_reprocess_unknown_kind(session):
    with pytest.raises(ValidationError, match="알 수 없는 staging"):
        await reprocess_staging(1, kind="bogus", user=ADMIN, session=session)


# ── M-2: FAILED 가시화 ──────────────────────────────────────
async def test_list_failed_surfaces_day_closed(session):
    await _failed_payment(session, source_ref="PAY-2", reason="day_closed")
    await _failed_payment(session, source_ref="PAY-3", reason="unmapped_chart_no")
    out = await list_failed_staging(kind="his_payment", user=ADMIN, session=session)
    assert out["entity"] == "integration.his_payment_staging"
    reasons = {i["ref"]: i["error_detail"]["reason"] for i in out["items"]}
    assert reasons == {"PAY-2": "day_closed", "PAY-3": "unmapped_chart_no"}
