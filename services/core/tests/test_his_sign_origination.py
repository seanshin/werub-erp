"""e-sign #6 ERP 의료계약 발의(origination) caller — snapshot 빌드·HIS 호출·미러 갱신.

확정 스키마(HIS PR#106): POST /sign-integration/erp/request-sign(X-Integration-Key 전용키) →
data{issueNo·signatureRequestId·...}. 미설정 시 비활성. 완료 미러는 sign.completed(기존 LIVE).
"""
from datetime import date

import pytest

from erp.config import get_settings
from erp.errors import ValidationError
from erp.hr.models import Employee, MedicalStaffContract
from erp.integrations import his_sign_origination as orig


async def _contract(session, **kw):
    emp = Employee(employee_no="DR9", name="홍길동", position="정형외과 전문의",
                   job_category="DOCTOR", hired_on=date(2024, 1, 1))
    session.add(emp)
    await session.flush()
    defaults = dict(employee_id=emp.id, valid_from=date(2026, 7, 1),
                    valid_to=date(2027, 6, 30), base_pay=15_000_000, exclusive=True)
    defaults.update(kw)
    c = MedicalStaffContract(**defaults)
    session.add(c)
    await session.flush()
    return c, emp


def test_build_medical_snapshot(session_unused=None):
    emp = Employee(employee_no="DR1", name="김의사", position="내과 전문의",
                   job_category="DOCTOR", hired_on=date(2024, 1, 1))
    c = MedicalStaffContract(id=42, employee_id=1, valid_from=date(2026, 7, 1),
                             valid_to=date(2027, 6, 30), base_pay=12_000_000,
                             exclusive=False, note="기타는 병원 규정에 따름")
    snap = orig._build_medical_snapshot(c, emp)
    assert snap["title"] == "의료계약서" and snap["contractNo"] == "ERP-MC-42"
    assert snap["parties"][0]["name"] == get_settings().org_legal_name
    assert snap["parties"][1] == {"role": "의료진", "name": "김의사", "position": "내과 전문의"}
    assert snap["period"] == {"start": "2026-07-01", "end": "2027-06-30"}
    assert snap["compensation"] == "월 12,000,000원"
    assert {"label": "전속여부", "value": "비전속"} in snap["fields"]
    assert snap["note"] == "기타는 병원 규정에 따름"


def test_snapshot_omits_optional_when_missing():
    emp = Employee(employee_no="DR2", name="무직급", job_group="STAFF",
                   hired_on=date(2024, 1, 1))
    c = MedicalStaffContract(id=7, employee_id=2, valid_from=date(2026, 1, 1),
                             base_pay=5_000_000, exclusive=True)
    snap = orig._build_medical_snapshot(c, emp)
    assert "position" not in snap["parties"][1]      # 직급 없음 → 생략
    assert snap["period"]["end"] is None             # valid_to 없음
    assert "note" not in snap                          # note 없음
    assert snap["fields"][0]["value"] == "STAFF"      # job_category 폴백 → job_group


@pytest.fixture
def _key(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "his_sign_origination_key", "erp_sign_orig_k", raising=False)
    return s


async def test_request_sign_disabled_without_key(session, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "his_sign_origination_key", None, raising=False)
    with pytest.raises(ValidationError, match="미설정"):
        await orig.request_sign(document_type="ERP_MEDICAL_CONTRACT", source_id=1, snapshot={})


async def test_request_sign_posts_with_key(_key, monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": {"issueNo": "WRB-1", "signatureRequestId": "SR-1"}}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json, headers):
            captured.update(url=url, json=json, headers=headers)
            return _Resp()

    monkeypatch.setattr(orig.httpx, "AsyncClient", _Client)
    data = await orig.request_sign(document_type="ERP_MEDICAL_CONTRACT", source_id=42,
                                   snapshot={"title": "x"}, channel="DIRECT")
    assert data["signatureRequestId"] == "SR-1"
    assert captured["url"].endswith("/api/v1/sign-integration/erp/request-sign")
    assert captured["headers"]["X-Integration-Key"] == "erp_sign_orig_k"
    assert captured["json"]["sourceId"] == 42 and captured["json"]["documentType"] == "ERP_MEDICAL_CONTRACT"
    assert "signers" not in captured["json"]      # 미지정 시 생략


def _mock_client(captured):
    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": {"issueNo": "WRB-1", "signatureRequestId": "SR-1"}}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json, headers):
            captured.update(url=url, json=json, headers=headers)
            return _Resp()
    return _Client


async def test_request_sign_maps_staff_envelope_to_direct(_key, monkeypatch):
    """봉투 채널: ERP STAFF는 HIS 전송 시 DIRECT로 매핑(HIS envelope 채널은 DIRECT|PORTAL뿐)."""
    captured = {}
    monkeypatch.setattr(orig.httpx, "AsyncClient", _mock_client(captured))
    await orig.request_sign(document_type="ERP_MEDICAL_CONTRACT", source_id=1,
                            snapshot={}, channel="STAFF")
    assert captured["json"]["channel"] == "DIRECT"


async def test_request_sign_staff_multiparty_verbatim(_key, monkeypatch):
    """다자 signers[]는 verbatim 전달 — STAFF 마커·externalSignerRef 보존(HIS prepareSigners가 cert 주입)."""
    captured = {}
    monkeypatch.setattr(orig.httpx, "AsyncClient", _mock_client(captured))
    signers = [{"displayName": "홍길동", "channel": "STAFF", "signOrder": 1,
                "signerType": "STAFF", "requiredIdentityLevel": "STAFF_SSO",
                "externalSignerRef": "erp:staff:DR9"},
               {"displayName": "병원장", "channel": "PORTAL", "signOrder": 2,
                "externalSignerRef": "erp:cosigner:1"}]
    await orig.request_sign(document_type="ERP_MEDICAL_CONTRACT", source_id=1,
                            snapshot={}, channel="STAFF", signers=signers)
    assert captured["json"]["channel"] == "DIRECT"          # envelope 매핑
    assert captured["json"]["signers"] == signers           # per-signer 무변경(verbatim)


async def test_initiate_updates_mirror(session, _key, monkeypatch):
    c, _emp = await _contract(session)

    async def _fake(**kw):
        assert kw["source_id"] == c.id and kw["document_type"] == "ERP_MEDICAL_CONTRACT"
        assert kw["snapshot"]["contractNo"] == f"ERP-MC-{c.id}"
        return {"issueNo": "WRB-MC-1", "signatureRequestId": "SR-99", "channel": "DIRECT",
                "portalUrl": None}

    monkeypatch.setattr(orig, "request_sign", _fake)
    out = await orig.initiate_medical_contract_signing(session, actor=1, contract_id=c.id)
    await session.flush()
    assert out["signature_request_id"] == "SR-99" and out["status"] == "SENT"
    assert c.sign_envelope_id == "SR-99" and c.sign_status == "SENT"
    assert c.sign_doc_ref == "WRB-MC-1"


async def test_initiate_rejects_completed(session, _key, monkeypatch):
    c, _emp = await _contract(session, sign_status="COMPLETED")
    monkeypatch.setattr(orig, "request_sign", lambda **k: None)
    with pytest.raises(ValidationError, match="완료"):
        await orig.initiate_medical_contract_signing(session, actor=1, contract_id=c.id)


async def test_initiate_unknown_contract(session, _key):
    with pytest.raises(ValidationError, match="의료계약"):
        await orig.initiate_medical_contract_signing(session, actor=1, contract_id=999999)


async def test_initiate_idempotent_guard(session, _key, monkeypatch):
    """SENT 재발의 — force 없으면 기존 봉투 반환(HIS 미호출), force=True면 새 봉투."""
    c, _emp = await _contract(session, sign_status="SENT", sign_envelope_id="EXIST-1")

    calls = {"n": 0}

    async def _fake(**kw):
        calls["n"] += 1
        return {"signatureRequestId": "NEW-2", "issueNo": "WRB-2", "channel": "PORTAL"}

    monkeypatch.setattr(orig, "request_sign", _fake)
    # force 없음 → no-op(기존 반환)
    out = await orig.initiate_medical_contract_signing(session, actor=1, contract_id=c.id)
    assert out["already_sent"] is True and out["signature_request_id"] == "EXIST-1"
    assert calls["n"] == 0                       # HIS 미호출(중복 봉투 방지)
    # force=True → 새 봉투
    out2 = await orig.initiate_medical_contract_signing(session, actor=1, contract_id=c.id,
                                                        force=True)
    await session.flush()
    assert calls["n"] == 1 and out2["signature_request_id"] == "NEW-2"
    assert c.sign_envelope_id == "NEW-2"


async def test_initiate_passes_signers(session, _key, monkeypatch):
    """다자 봉투 — signers[]가 request_sign(→HIS 패스스루)에 전달."""
    c, _emp = await _contract(session)
    captured = {}

    async def _fake(**kw):
        captured.update(kw)
        return {"signatureRequestId": "SR-M", "issueNo": "WRB-M", "channel": "PORTAL"}

    monkeypatch.setattr(orig, "request_sign", _fake)
    signers = [{"displayName": "홍길동", "channel": "PORTAL", "signOrder": 1},
               {"displayName": "병원장", "channel": "PORTAL", "signOrder": 2}]
    await orig.initiate_medical_contract_signing(session, actor=1, contract_id=c.id,
                                                 signers=signers)
    assert captured["signers"] == signers


async def test_initiate_returns_per_signer_portal_links(session, _key, monkeypatch):
    """다자 PORTAL — HIS 응답 signers[].portalUrl을 portal_links로 반환(운영자 발송용, D3)."""
    c, _emp = await _contract(session)

    async def _fake(**kw):
        return {"signatureRequestId": "SR-M", "issueNo": "WRB-M", "channel": "PORTAL",
                "portalUrl": None,
                "signers": [
                    {"signOrder": 1, "channel": "PORTAL", "displayName": "홍길동",
                     "externalSignerRef": "erp:staff:BF-2026-0001",
                     "portalUrl": "https://sign.example.org/portal/t1"},
                    {"signOrder": 2, "channel": "PORTAL", "displayName": "병원장",
                     "externalSignerRef": "erp:cosigner:1",
                     "portalUrl": "https://sign.example.org/portal/t2"}]}

    monkeypatch.setattr(orig, "request_sign", _fake)
    out = await orig.initiate_medical_contract_signing(session, actor=1, contract_id=c.id,
                                                       signers=[{"x": 1}])
    links = out["portal_links"]
    assert [link["sign_order"] for link in links] == [1, 2]
    assert links[0]["portal_url"].endswith("/t1") and links[1]["display_name"] == "병원장"


async def test_initiate_rejects_bad_signers(session, _key, monkeypatch):
    c, _emp = await _contract(session)
    monkeypatch.setattr(orig, "request_sign", lambda **k: None)
    with pytest.raises(ValidationError, match="signers"):
        await orig.initiate_medical_contract_signing(session, actor=1, contract_id=c.id,
                                                     signers=["notadict"])
