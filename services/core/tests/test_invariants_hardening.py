"""배치중립 Phase0 감사 보완(v1.208) — egress 가드·AI 검색 PII 강등·감사 커버리지."""
from datetime import date

import pytest

from erp.errors import ValidationError


async def test_sign_origination_host_guard(monkeypatch):
    """의료계약 발의(직원 보수 페이로드) — 허용 외 호스트로는 발신 자체 차단(INV-NET-01)."""
    from erp.config import get_settings
    from erp.integrations.his_sign_origination import request_sign
    s = get_settings()
    monkeypatch.setattr(s, "his_sign_origination_key", "k", raising=False)
    monkeypatch.setattr(s, "his_api_base", "https://evil.example.com", raising=False)
    with pytest.raises(ValidationError, match="허용되지 않은"):
        await request_sign(document_type="MEDICAL_CONTRACT", source_id=1, snapshot={})


async def test_ai_search_pii_query_degrades_to_local(session, client, monkeypatch):
    """ai-search 검색어에 실 차트번호 포함 시 외부 AI 미전송 — 로컬 검색 강등(+note)."""
    from erp.config import get_settings
    from erp.patient.models import Patient
    from tests._http import auth_as

    monkeypatch.setattr(get_settings(), "weve_ai_api_key", "test-key", raising=False)
    session.add(Patient(id=9, chart_no="HR-77001", name="김환자", birth_date=date(1990, 1, 1)))
    await session.flush()
    headers = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.get("/api/v1/regulatory/notices/ai-search?q=HR-77001 진찰료",
                         headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "local" and "미전송" in body["note"]   # 외부 호출 없이 강등


async def test_patient_search_post_no_query_string(session, client):
    """W1 — POST /patients/search: 식별자 body 전달(쿼리스트링 미노출)·GET과 동일 결과."""
    from erp.patient.models import Patient
    from tests._http import auth_as
    session.add(Patient(id=11, chart_no="HR-88001", name="박환자", birth_date=date(1985, 3, 2)))
    await session.flush()
    headers = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.post("/api/v1/patients/search", headers=headers,
                          json={"chart_no": "HR-88001"})
    assert r.status_code == 200 and r.json()["items"][0]["name"] == "박환자"
    r2 = await client.post("/api/v1/claims/pre-review/cases/search", headers=headers,
                           json={"chart_no": "HR-88001"})
    assert r2.status_code == 200 and r2.json()["count"] == 0    # 워크리스트 POST 검색 동작


async def test_overdue_and_dunning_access_logged(session, client):
    """채권 열람 접속기록(G4) — overdue 목록·독촉 이력 조회가 access_log에 남는다."""
    from sqlalchemy import select

    from erp.audit.models import AccessLog
    from tests._http import auth_as
    headers = await auth_as(session, user_id=1, role="ADMIN")
    assert (await client.get("/api/v1/patients/receivables/overdue",
                             headers=headers)).status_code == 200
    row = await session.scalar(select(AccessLog).where(
        AccessLog.entity == "patient.receivables"))
    assert row is not None and row.actor == 1