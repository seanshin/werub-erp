"""ITAM 자산 폐기 (P2-12 커버리지) — PHI 소거 게이트·F-3 결재 게이트·즉시폐기 폴백.

기존 test_itam은 폐기(dispose/disposal_approval/disposal_status)를 미검증. 폐기는 결재선·PHI 안전
게이트가 걸린 민감 경로라 별도 커버.
"""
from datetime import date

import pytest

from erp.common.models import ApprovalRule, User
from erp.errors import ValidationError
from erp.itam.service import ITAMService

svc = ITAMService()


async def test_dispose_immediate_when_no_rule(session):
    """폐기 결재 규칙이 없으면 즉시 폐기(레거시 폴백). 이중 폐기는 차단."""
    a = await svc.create_asset(session, actor=1, asset_tag="D-1", name="폐기랩탑")
    out = await svc.dispose(session, actor=1, asset_id=a.id, data_erased=True,
                            on=date(2026, 6, 1))
    assert out.status == "DISPOSED" and out.disposed_at == date(2026, 6, 1)
    with pytest.raises(ValidationError, match="이미 폐기"):
        await svc.dispose(session, actor=1, asset_id=a.id, data_erased=True)


async def test_dispose_phi_erasure_gate(session):
    """PHI 취급(또는 과거 취급) 장비는 데이터 소거 확인 후에만 폐기(S-2)."""
    a = await svc.create_asset(session, actor=1, asset_tag="D-PHI", name="영상워크스테이션",
                               handles_phi=True)
    with pytest.raises(ValidationError, match="데이터 소거"):
        await svc.dispose(session, actor=1, asset_id=a.id, data_erased=False)
    out = await svc.dispose(session, actor=1, asset_id=a.id, data_erased=True)
    assert out.status == "DISPOSED" and out.data_erased is True


async def test_dispose_approval_gate_then_finalize(session):
    """F-3 — ITAM_DISPOSAL 규칙 존재 시 승인 후에만 폐기. 직무분리(작성자≠결재자)."""
    session.add(ApprovalRule(doc_type="ITAM_DISPOSAL", min_amount=0, max_amount=None,
                             steps=[{"type": "ROLE", "value": "MANAGER"}], active=True))
    manager = User(id=2, email="itam-mgr@x.example", password_hash="x", role="MANAGER")
    session.add(manager)
    await session.flush()

    a = await svc.create_asset(session, actor=1, asset_tag="D-APPR", name="결재폐기장비")
    # 규칙 매칭 → 즉시 폐기 불가·결재선 상신
    with pytest.raises(ValidationError, match="결재 승인이 필요"):
        await svc.dispose(session, actor=1, asset_id=a.id, data_erased=True)
    assert (await svc.disposal_status(session, a.id))["status"] == "PENDING"

    # 작성자 본인 승인 불가(직무분리)
    with pytest.raises(ValidationError, match="작성자"):
        await svc.disposal_approval(session, actor=1, asset_id=a.id, approve=True)
    # MANAGER 승인 → APPROVED
    res = await svc.disposal_approval(session, actor=2, asset_id=a.id, approve=True)
    assert res["status"] == "APPROVED"

    # 승인 후 재폐기 → 확정
    out = await svc.dispose(session, actor=1, asset_id=a.id, data_erased=True)
    assert out.status == "DISPOSED"
