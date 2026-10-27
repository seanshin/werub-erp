"""전자결재(HIS W.Sign 릴레이) — HMAC 검증·콜백 반영·상신."""
import hashlib
import hmac
from datetime import date

import httpx
from sqlalchemy import func, select

from erp.common.models import Notification, User
from erp.config import get_settings
from erp.hr.eapproval_service import EApprovalService
from erp.hr.models import Employee, LeaveRequest
from erp.integrations.eapproval import (
    EApprovalClient, callback_replay_reason, verify_signature,
)


def test_client_unwraps_data_envelope():
    def handler(request):
        assert request.headers["X-Integration-Key"] == "k"
        assert request.headers["Idempotency-Key"] == "hr.leave_requests:1"
        return httpx.Response(200, json={"data": {
            "approval_id": "wsign-9", "view_url": "https://his/wsign-9", "status": "PENDING"}})

    client = EApprovalClient("https://his.example.org", "k", "WRB-DEMO-M66M",
                             transport=httpx.MockTransport(handler))
    out = client.submit({"ref": {"source_module": "hr.leave_requests", "source_ref": "1"}},
                        path="/api/v1/erp/eapproval/submit")
    assert out["approval_id"] == "wsign-9"        # res.data.approval_id 언랩
    assert out["view_url"].endswith("/wsign-9")


def test_verify_signature():
    secret, body = "s3cr3t", b'{"a":1}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, secret) is True
    assert verify_signature(body, "sha256=deadbeef", secret) is False
    assert verify_signature(body, None, secret) is False
    assert verify_signature(body, sig, "") is False


def test_callback_replay_reason():
    """콜백 재생 방지 — 서명된 ts 신선도 검사 (security-architect MEDIUM)."""
    now = 1_700_000_000.0
    # 신선한 ts → 통과
    assert callback_replay_reason({"ts": now - 10}, now_epoch=now, max_skew=300) is None
    assert callback_replay_reason({"ts": now + 10}, now_epoch=now, max_skew=300) is None
    # 창 초과(캡처 후 재전송) → 거부
    assert callback_replay_reason({"ts": now - 600}, now_epoch=now, max_skew=300) is not None
    # ts 누락(검사 우회 시도) → 거부
    assert callback_replay_reason({"result": "APPROVED"}, now_epoch=now, max_skew=300) is not None
    # ts 형식 오류 → 거부
    assert callback_replay_reason({"ts": "bad"}, now_epoch=now, max_skew=300) is not None
    # 경계값(정확히 max_skew) → 통과
    assert callback_replay_reason({"ts": now - 300}, now_epoch=now, max_skew=300) is None


async def _leave(session, **kw):
    emp = Employee(employee_no=kw.pop("no", "L1"), name=kw.pop("name", "홍길동"),
                   hired_on=date(2020, 1, 1), department_id=1, job_group="NURSE",
                   external_user_id=kw.pop("ext", None))
    session.add(emp)
    await session.flush()
    lv = LeaveRequest(employee_id=emp.id, leave_type="ANNUAL", start_on=date(2026, 7, 1),
                      end_on=date(2026, 7, 2), days=2, status="REQUESTED")
    session.add(lv)
    await session.flush()
    return emp, lv


async def test_callback_approves_leave_and_notifies(session):
    emp, lv = await _leave(session)
    u = User(email="l1@x.example", password_hash="x", role="STAFF", employee_id=emp.id)
    session.add(u)
    await session.flush()
    out = await EApprovalService().handle_callback(session, body={
        "ref": {"source_module": "hr.leave_requests", "source_ref": str(lv.id)},
        "result": "APPROVED"})
    assert out["status"] == "APPROVED"
    assert lv.status == "APPROVED" and lv.decided_at is not None
    n = await session.scalar(select(func.count()).select_from(Notification)
                             .where(Notification.user_id == u.id, Notification.type == "APPROVAL"))
    assert n == 1


async def test_callback_rejected_maps_status(session):
    _emp, lv = await _leave(session, no="L9")
    await EApprovalService().handle_callback(session, body={
        "ref": {"source_module": "hr.leave_requests", "source_ref": str(lv.id)},
        "result": "REJECTED"})
    assert lv.status == "REJECTED"


async def test_callback_unknown_ref_ignored(session):
    out = await EApprovalService().handle_callback(session, body={
        "ref": {"source_module": "other"}, "result": "APPROVED"})
    assert out["ignored"] is True


async def test_submit_leave_stores_approval(session):
    class FakeClient:
        def submit(self, payload, *, path):
            assert payload["doc_type"] == "HR_LEAVE"
            assert payload["ref"]["source_module"] == "hr.leave_requests"
            assert payload["drafter_clinic_user_id"] == "clinic-1"
            return {"approval_id": "wsign-1", "view_url": "https://his.example.org/wsign-1"}

    _emp, lv = await _leave(session, no="L2", ext="clinic-1")
    s = get_settings()
    old, olden = s.his_integration_key, s.eapproval_submit_enabled
    s.his_integration_key, s.eapproval_submit_enabled = "erpik_test", True
    try:
        resp = await EApprovalService(client=FakeClient()).submit_leave(session, leave=lv)
    finally:
        s.his_integration_key, s.eapproval_submit_enabled = old, olden
    assert resp["approval_id"] == "wsign-1"
    assert lv.approval_id == "wsign-1" and lv.view_url.endswith("/wsign-1")


async def test_submit_disabled_when_unset(session):
    _emp, lv = await _leave(session, no="L3")
    s = get_settings()
    old = s.his_integration_key
    s.his_integration_key = None
    try:
        assert await EApprovalService().submit_leave(session, leave=lv) is None
    finally:
        s.his_integration_key = old


async def test_callback_idempotent_on_replay(session):
    from erp.hr.eapproval_service import EApprovalService
    _, lv = await _leave(session, no="LIDEM")
    body = {"ref": {"source_module": "hr.leave_requests", "source_ref": str(lv.id)},
            "result": "APPROVED"}
    out1 = await EApprovalService().handle_callback(session, body=body)
    assert out1.get("updated")
    out2 = await EApprovalService().handle_callback(session, body=body)   # 재생
    assert out2.get("ignored") and out2.get("reason") == "already processed"


async def test_callback_ignores_non_pending(session):
    from erp.hr.eapproval_service import EApprovalService
    _, lv = await _leave(session, no="LNP")
    lv.status = "CANCELED"
    await session.flush()
    out = await EApprovalService().handle_callback(session, body={
        "ref": {"source_module": "hr.leave_requests", "source_ref": str(lv.id)},
        "result": "APPROVED"})   # 취소건에 stale 승인 재생 → 무시
    assert out.get("ignored") and lv.status == "CANCELED"
