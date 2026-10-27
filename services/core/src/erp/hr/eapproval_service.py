"""전자결재 오케스트레이션 (HR) — 휴가 상신(ERP→HIS) + 콜백 처리(HIS→ERP).

상신: 직원 external_user_id(=Clinic userId)를 기안자로 HIS W.Sign 릴레이에 상신.
콜백: 결과(APPROVED/REJECTED/WITHDRAWN)를 hr.leave_requests status에 반영 + 신청자 알림.
his_integration_key 미설정 시 상신 비활성(ERP 자체 승인 흐름 유지).
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.models import Notification, User
from erp.config import get_settings
from erp.hr.models import Employee, LeaveRequest
from erp.integrations.eapproval import EApprovalClient

logger = logging.getLogger("erp.hr.eapproval")
_RESULT_MAP = {"APPROVED": "APPROVED", "REJECTED": "REJECTED", "WITHDRAWN": "CANCELED"}


class EApprovalService:
    def __init__(self, client: EApprovalClient | None = None):
        self._client = client   # 테스트 주입

    def _client_or_none(self):
        s = get_settings()
        # 상신은 플래그+키 모두 필요 (HIS 엔드포인트 경로 확정 후 활성). 콜백 수신은 무관.
        if not (s.eapproval_submit_enabled and s.his_integration_key):
            return None
        return self._client or EApprovalClient(
            s.his_eapproval_base, s.his_integration_key, s.hospital_code)

    async def submit_leave(self, session: AsyncSession, *, leave: LeaveRequest) -> dict | None:
        s = get_settings()
        client = self._client_or_none()
        if client is None:
            return None
        emp = await session.get(Employee, leave.employee_id)
        callback = (s.eapproval_callback_url
                    or f"{s.web_origin}/api/v1/integrations/eapproval/callback")
        payload = {
            "doc_type": "HR_LEAVE",
            "title": f"휴가 신청 ({emp.name if emp else leave.employee_id}, "
                     f"{leave.start_on}~{leave.end_on})",
            "drafter_clinic_user_id": emp.external_user_id if emp else None,
            "approvers": None,                       # HIS 조직도 결재선 자동추천
            "content": {"leave_type": leave.leave_type, "start_on": str(leave.start_on),
                        "end_on": str(leave.end_on), "days": float(leave.days),
                        "reason": leave.reason},
            "ref": {"source_module": "hr.leave_requests", "source_ref": str(leave.id)},
            "callback_url": callback,
        }
        resp = await asyncio.to_thread(client.submit, payload, path=s.eapproval_submit_path)
        leave.approval_id = resp.get("approval_id")
        leave.view_url = resp.get("view_url")
        logger.info("휴가 결재 상신", extra={"ctx": {"leave": leave.id,
                    "approval_id": leave.approval_id}})
        return resp

    async def handle_callback(self, session: AsyncSession, *, body: dict) -> dict:
        ref = body.get("ref") or {}
        if ref.get("source_module") != "hr.leave_requests":
            return {"ignored": True, "reason": "unknown ref"}
        try:
            leave_id = int(ref.get("source_ref", 0))
        except (TypeError, ValueError):
            return {"ignored": True, "reason": "bad ref"}
        leave = await session.get(LeaveRequest, leave_id)
        if leave is None:
            return {"ignored": True, "reason": "not found"}
        result = body.get("result")
        new_status = _RESULT_MAP.get(result)
        if not new_status:
            return {"ignored": True, "reason": "unknown result"}
        # 멱등·재생 방지 — 이미 처리됐거나 대기(REQUESTED)가 아니면 무시(취소건 되돌림·중복알림 차단)
        if leave.status == new_status:
            return {"ignored": True, "reason": "already processed", "leave_id": leave_id}
        if leave.status != "REQUESTED":
            return {"ignored": True, "reason": f"not pending ({leave.status})", "leave_id": leave_id}
        leave.status = new_status
        leave.decided_at = datetime.now(timezone.utc)
        uid = await session.scalar(select(User.id).where(User.employee_id == leave.employee_id))
        if uid:
            session.add(Notification(user_id=uid, type="APPROVAL",
                        title=f"휴가 결재 {result}",
                        body=f"{leave.start_on}~{leave.end_on} 휴가가 {result} 처리되었습니다."))
        logger.info("휴가 결재 콜백", extra={"ctx": {"leave": leave_id, "result": result}})
        # ⚠ HIS로 결과를 되돌려 보내지 않는다. 종전 `_relay_to_his`는 ERP 휴가
        #    정수 ID를 HIS ESS 콜백에 실었는데, HIS는 자기 휴가 UUID로 찾으므로 늘 400/404였다 —
        #    ERP발 휴가는 HIS에 휴가 행이 없다. 그리고 이 콜백을 보낸 것이 HIS 자신이며 전달 전에
        #    자기 릴레이 원장(e_approval_relays)에 결과를 이미 적는다. 되돌려 보낼 대상이 없다.
        return {"updated": True, "leave_id": leave_id, "status": leave.status}
