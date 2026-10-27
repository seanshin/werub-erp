"""결정 대기함(0226) — 등록·결정·보류·재오픈 생애주기·RBAC·감사로그."""
from sqlalchemy import select

from erp.common.models import PendingDecision

_OPTS = [{"key": "A", "label": "안 A", "description": "권고", "recommended": True},
         {"key": "B", "label": "안 B", "description": "대안"}]


async def _seed(session, key="d-1"):
    d = PendingDecision(key=key, title="테스트 결정", question="A vs B?", options=_OPTS,
                        category="ARCH")
    session.add(d)
    await session.flush()
    return d


async def test_decide_lifecycle_and_audit(session, client):
    """결정 기록 → 재결정 차단 → 재오픈(사유 필수) → 재결정. 감사로그 잔존."""
    from erp.audit.models import AuditLog
    from tests._http import auth_as
    d = await _seed(session)
    headers = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.post(f"/api/v1/admin/decisions/{d.id}/decide", headers=headers,
                          json={"option": "A", "note": "권고안 수용"})
    assert r.status_code == 200 and r.json()["status"] == "DECIDED"
    # 유효하지 않은 선택지·재결정 차단
    d2 = await _seed(session, key="d-2")
    assert (await client.post(f"/api/v1/admin/decisions/{d2.id}/decide", headers=headers,
                              json={"option": "X"})).status_code == 422
    assert (await client.post(f"/api/v1/admin/decisions/{d.id}/decide", headers=headers,
                              json={"option": "B"})).status_code == 422
    # 재오픈 — 사유 필수·PENDING 복귀·감사로그에 이전 결정 잔존
    assert (await client.post(f"/api/v1/admin/decisions/{d.id}/reopen", headers=headers,
                              json={"note": ""})).status_code == 422
    r = await client.post(f"/api/v1/admin/decisions/{d.id}/reopen", headers=headers,
                          json={"note": "조건 변경"})
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    logs = (await session.scalars(select(AuditLog).where(
        AuditLog.entity == "common.pending_decisions", AuditLog.target_id == "d-1"))).all()
    assert {log.action for log in logs} >= {"DECIDE", "REOPEN"}


async def test_defer_and_list_ordering(session, client):
    from tests._http import auth_as
    headers = await auth_as(session, user_id=1, role="DIRECTOR")   # DIRECTOR도 write
    a = await _seed(session, key="d-a")
    await _seed(session, key="d-b")
    r = await client.post(f"/api/v1/admin/decisions/{a.id}/defer", headers=headers,
                          json={"note": "실배포 시점 일괄"})
    assert r.status_code == 200 and r.json()["status"] == "DEFERRED"
    r = await client.get("/api/v1/admin/decisions", headers=headers)
    items = r.json()["items"]
    assert [i["key"] for i in items] == ["d-b", "d-a"]   # PENDING 우선
    r = await client.get("/api/v1/admin/decisions?status=pending", headers=headers)
    assert [i["key"] for i in r.json()["items"]] == ["d-b"]


async def test_create_and_rbac(session, client):
    """수동 등록(중복 key 거부)·STAFF 접근 차단."""
    from tests._http import auth_as
    headers = await auth_as(session, user_id=1, role="ADMIN")
    body = {"key": "new-1", "title": "신규", "question": "?", "options": _OPTS}
    assert (await client.post("/api/v1/admin/decisions", headers=headers,
                              json=body)).status_code == 201
    assert (await client.post("/api/v1/admin/decisions", headers=headers,
                              json=body)).status_code == 422   # 중복 key
    staff = await auth_as(session, user_id=2, role="STAFF")
    assert (await client.get("/api/v1/admin/decisions", headers=staff)).status_code == 403

async def test_update_pending_but_not_decided(session, client):
    """질문·선택지 갱신 — **상황이 변하면 결정문도 낡는다**(구현 완료된 선택지를 계속 묻는 상태).

    ⚠ DECIDED는 갱신 금지: 무엇을 보고 결정했는지가 바뀌면 결정 근거가 사라진다(재오픈 후 갱신).
    """
    from erp.audit.models import AuditLog
    from tests._http import auth_as
    d = await _seed(session, key="d-upd")
    headers = await auth_as(session, user_id=1, role="ADMIN")
    new_opts = [{"key": "A", "label": "안 A(구현 완료)", "description": "v1.285 배포됨"},
                {"key": "C", "label": "안 C", "recommended": True}]
    r = await client.patch(f"/api/v1/admin/decisions/{d.id}", headers=headers,
                           json={"question": "A는 이미 구현됨 — C로 확장할까?",
                                 "options": new_opts})
    assert r.status_code == 200
    assert r.json()["question"].startswith("A는 이미 구현됨")
    assert [o["key"] for o in r.json()["options"]] == ["A", "C"]
    # 갱신 전/후가 감사로그에 남는다
    log = (await session.scalars(select(AuditLog).where(
        AuditLog.action == "UPDATE", AuditLog.target_id == "d-upd"))).all()
    assert len(log) == 1 and log[0].before["question"] == "A vs B?"
    # 빈/형식 오류 선택지 거부
    assert (await client.patch(f"/api/v1/admin/decisions/{d.id}", headers=headers,
                              json={"options": []})).status_code == 422
    assert (await client.patch(f"/api/v1/admin/decisions/{d.id}", headers=headers,
                              json={"options": [{"key": "X"}]})).status_code == 422
    # 결정된 항목은 갱신 불가(재오픈 후에만)
    await client.post(f"/api/v1/admin/decisions/{d.id}/decide", headers=headers,
                      json={"option": "C"})
    assert (await client.patch(f"/api/v1/admin/decisions/{d.id}", headers=headers,
                              json={"question": "사후 변경"})).status_code == 422
    # STAFF 차단
    staff = await auth_as(session, user_id=2, role="STAFF")
    assert (await client.patch(f"/api/v1/admin/decisions/{d.id}", headers=staff,
                              json={"question": "x"})).status_code == 403
