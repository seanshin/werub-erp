"""HIS 개방 피드 소비 — MEDRLT JSON 적재·직원 email 링크 (2026-06-12 합의)."""

from sqlalchemy import select

from erp.claims.ingest_service import ingest_medrlt_json
from erp.claims.models import ClaimAdjustment, ClaimStatement
from erp.claims.worklist_service import WorklistService
from erp.common.models import User
from erp.hr.models import Employee
from erp.integrations.inbound_consumer import _staff

SAMPLE = {
    "docType": "MEDRLT", "marker": "TEST-SYNTHETIC",
    "notice": {"noticeNo": "TEST-MEDRLT-001", "reviewYm": "202605",
               "noticedAt": "2026-06-11"},
    "statements": [
        {"chartNo": "P2026-0003", "statementSeq": 1, "result": "ADJUSTED",
         "claimedAmount": 69400, "reviewedAmount": 64400, "adjustedAmount": 5000,
         "adjustments": [{"lineCode": "AA257", "adjustCode": "A",
                          "adjustReasonCode": "A20",
                          "adjustReason": "산정기준 초과 — 심사조정",
                          "reducedAmount": 5000}]},
        {"chartNo": "P2026-0004", "statementSeq": 2, "result": "PASSED",
         "claimedAmount": 126850, "reviewedAmount": 126850, "adjustedAmount": 0,
         "adjustments": []},
    ],
    "summary": {"totalClaimed": 196250, "totalReduced": 5000},
}


async def test_medrlt_json_ingest_idempotent_and_history(session):
    out = await ingest_medrlt_json(session, payload=SAMPLE, actor=1)
    assert out["statements"] == 2 and out["adjustments"] == 1
    assert out["synthetic"] is True

    dup = await ingest_medrlt_json(session, payload=SAMPLE, actor=1)
    assert dup["duplicate"] is True                       # receipt_no 멱등

    stmt = (await session.scalars(select(ClaimStatement).where(
        ClaimStatement.chart_no == "P2026-0003"))).one()
    adj = (await session.scalars(select(ClaimAdjustment).where(
        ClaimAdjustment.statement_id == stmt.id))).one()
    assert float(adj.adj_amt) == 5000 and adj.adj_code == "A20"
    assert adj.map_status == "AUTO"

    # E2E 핵심: 심사 케이스 컨텍스트(adjustment_history)가 chart_no로 매칭
    hist = await WorklistService().adjustment_history(session, chart_no="P2026-0003")
    assert hist["count"] == 1 and hist["total_amt"] == 5000
    assert hist["top_categories"][0]["category"] == "A"


async def test_medrlt_json_missing_receipt_raises_validation(session):
    """W3: noticeNo 누락 시 raw ValueError(500) 아닌 ValidationError(전역 핸들러 422)."""
    import pytest

    from erp.errors import ValidationError
    with pytest.raises(ValidationError):
        await ingest_medrlt_json(session, payload={"notice": {}}, actor=1)


async def test_staff_event_links_user_by_email(session):
    user = User(email="doc20@hospital.example.org", password_hash="x", role="STAFF")
    session.add(user)
    await session.flush()

    await _staff(session, {"employeeNumber": "DR-099", "name": "신의사",
                           "hireDate": "2026-06-01", "email": "DOC20@hospital.example.org"},
                 "staff.created", {})
    emp = (await session.scalars(select(Employee).where(
        Employee.employee_no == "DR-099"))).one()
    await session.refresh(user)
    assert user.employee_id == emp.id                     # 대소문자 무관 매칭

    # 기존 수동 연결 보존 — 다른 직원 이벤트가 덮어쓰지 않음
    await _staff(session, {"employeeNumber": "DR-100", "name": "또의사",
                           "hireDate": "2026-06-01", "email": "doc20@hospital.example.org"},
                 "staff.created", {})
    await session.refresh(user)
    assert user.employee_id == emp.id


async def test_staff_event_stores_email_for_jit(session):
    """0048 — staff 이벤트 email이 직원에 저장(향후 JIT 연결 키)."""
    await _staff(session, {"employeeNumber": "DR-101", "name": "이메일러",
                           "hireDate": "2026-06-01", "email": "Late.Login@hospital.example.org"},
                 "staff.created", {})
    emp = (await session.scalars(select(Employee).where(
        Employee.employee_no == "DR-101"))).one()
    assert emp.email == "late.login@hospital.example.org"             # 소문자 정규화 저장
