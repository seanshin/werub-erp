"""계약 전자서명 미러 (e-sign #6) — HIS sign.completed(도메인 sign) → 계약 미러필드 갱신.

HIS 실 payload(his-reply-esign6-completed-samples): documentType·signatureRequestId·status·
completedAt·certSerial·assuranceLevel·document.url. ERP는 ERP_* 계약만 미러, CONSENT 등은 무시.
"""
from datetime import date, timedelta

import pytest

from erp.hr.models import Employee, MedicalStaffContract
from erp.integrations.inbound_consumer import _sign_completed
from erp.scm.models import OutsourcingContract


async def _medical(session):
    emp = Employee(employee_no="DR1", name="의사", hired_on=date(2024, 1, 1))
    session.add(emp)
    await session.flush()
    c = MedicalStaffContract(employee_id=emp.id, valid_from=date(2026, 1, 1), base_pay=5_000_000)
    session.add(c)
    await session.flush()
    return c


def _payload(dtype, cid=None, **kw):
    # 기본 url은 비허용 호스트 → 회수(fetch) best-effort가 SSRF 가드로 즉시 skip(미러만 검증·무네트워크).
    p = {"documentType": dtype, "signatureRequestId": "SR-1", "status": "COMPLETED",
         "completedAt": "2026-06-28T02:05:11+00:00", "certSerial": "5B:1A",
         "assuranceLevel": "HSM_ANCHORED", "auditCertUri": "/v1/requests/SR-1/audit-certificate",
         "document": {"issueNo": "WRB-1", "url": "https://example.test/dl/WRB-1?t=x"}}
    if cid is not None:
        p["erpContractId"] = cid
    p.update(kw)
    return p


async def test_sign_completed_mirrors_medical_contract(session):
    c = await _medical(session)
    await _sign_completed(session, _payload("ERP_MEDICAL_CONTRACT", c.id), "sign.completed", {})
    await session.flush()
    assert c.sign_status == "COMPLETED" and c.sign_envelope_id == "SR-1"
    assert c.sign_cert_serial == "5B:1A" and c.sign_assurance == "HSM_ANCHORED"
    # sign_doc_ref는 영구 식별자(auditCertUri)만 — 24h 단기 토큰 URL은 저장 안 함(보안 #4).
    assert c.signed_at is not None and c.sign_doc_ref == "/v1/requests/SR-1/audit-certificate"
    assert "t=x" not in (c.sign_doc_ref or "")


async def test_sign_completed_enqueues_then_drains_to_attachment(session, tmp_path, monkeypatch):
    """완료본 회수(§2) — 미러는 아웃박스 enqueue만, job_esign_fetch 드레인이 fetch→Attachment 링크."""
    import erp.integrations.esign_fetch as ef
    from sqlalchemy import select

    from erp.common.models import Attachment
    from erp.config import get_settings
    from erp.integrations.esign_fetch import process_esign_fetch_queue
    from erp.integrations.models import EsignFetchOutbox
    s = get_settings()
    monkeypatch.setattr(s, "attachment_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(s, "attachment_max_bytes", 10 * 1024 * 1024, raising=False)

    class _Resp:
        content = b"%PDF-1.4 signed"
        def raise_for_status(self): pass

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _Resp()

    monkeypatch.setattr(ef.httpx, "AsyncClient", _Client)
    c = await _medical(session)
    p = _payload("ERP_MEDICAL_CONTRACT", c.id)
    p["document"]["url"] = "https://his.example.org/api/v1/documents/dl/WRB-1?t=x"  # 허용 호스트
    # 1) 미러 — enqueue만(아직 fetch·attachment 없음)
    await _sign_completed(session, p, "sign.completed", {})
    await session.flush()
    assert c.sign_doc_attachment_id is None
    q = (await session.execute(select(EsignFetchOutbox).where(
        EsignFetchOutbox.entity_type == "medical_contract",
        EsignFetchOutbox.contract_id == c.id))).scalar_one()
    assert q.status == "PENDING"
    # 2) 드레인 — fetch→Attachment 링크·DONE
    out = await process_esign_fetch_queue(session)
    await session.flush()
    assert out["done"] == 1
    assert c.sign_doc_attachment_id is not None and q.status == "DONE"
    att = (await session.execute(select(Attachment).where(
        Attachment.id == c.sign_doc_attachment_id))).scalar_one()
    assert att.entity_type == "contract" and att.entity_id == c.id
    assert att.content_type == "application/pdf" and att.sha256


async def test_esign_fetch_queue_retry_on_failure(session):
    """드레인 회수 실패 — 비허용 호스트(SSRF 가드)면 FAILED·백오프(미러 비차단)."""
    from sqlalchemy import select

    from erp.integrations.esign_fetch import process_esign_fetch_queue
    from erp.integrations.models import EsignFetchOutbox
    c = await _medical(session)
    p = _payload("ERP_MEDICAL_CONTRACT", c.id)  # 기본 url=example.test(비허용 호스트)
    await _sign_completed(session, p, "sign.completed", {})
    await session.flush()
    out = await process_esign_fetch_queue(session)
    assert out["failed"] == 1
    q = (await session.execute(select(EsignFetchOutbox).where(
        EsignFetchOutbox.contract_id == c.id))).scalar_one()
    assert q.status == "FAILED" and q.retry_count == 1 and q.next_retry_at is not None
    assert c.sign_doc_attachment_id is None     # 미러는 유지(회수만 실패)


async def test_esign_fetch_retry_limit_goes_dead(session):
    """G10 — 한도 도달 시 DEAD. 종전엔 next_retry_at=None인 FAILED라 조회가 다시 집어 무한 재시도."""
    from sqlalchemy import select

    from erp.integrations import esign_fetch
    from erp.integrations.models import EsignFetchOutbox
    c = await _medical(session)
    await _sign_completed(session, _payload("ERP_MEDICAL_CONTRACT", c.id), "sign.completed", {})
    await session.flush()
    q = (await session.execute(select(EsignFetchOutbox).where(
        EsignFetchOutbox.contract_id == c.id))).scalar_one()
    q.retry_count = esign_fetch._FETCH_MAX_RETRY - 1
    await session.flush()
    await esign_fetch.process_esign_fetch_queue(session)
    assert q.status == "DEAD"
    assert (await esign_fetch.process_esign_fetch_queue(session)) == {"done": 0, "failed": 0}


async def test_esign_fetch_dead_notifies_active_admins(session):
    """H8 — DEAD 전이 시 활성 관리자 알림(사람이 HIS에 재발급 요청해야 함)."""
    from sqlalchemy import select

    from erp.common.models import Notification, User
    from erp.integrations import esign_fetch
    from erp.integrations.models import EsignFetchOutbox
    session.add_all([User(email="dead-admin@x.example", password_hash="x", role="ADMIN", is_active=True),
                     User(email="gone-admin@x.example", password_hash="x", role="ADMIN", is_active=False)])
    c = await _medical(session)
    await _sign_completed(session, _payload("ERP_MEDICAL_CONTRACT", c.id), "sign.completed", {})
    await session.flush()
    q = (await session.execute(select(EsignFetchOutbox).where(
        EsignFetchOutbox.contract_id == c.id))).scalar_one()
    q.retry_count = esign_fetch._FETCH_MAX_RETRY - 1
    await session.flush()
    await esign_fetch.process_esign_fetch_queue(session)
    await session.flush()
    n = (await session.execute(select(Notification).where(
        Notification.type == "ESIGN_FETCH_DEAD"))).scalars().all()
    assert len(n) == 1


async def test_external_signer_ref_linkage(session):
    """erpContractId 대신 externalSignerRef='erpcontract:medical:<id>'로도 링크."""
    c = await _medical(session)
    p = _payload("ERP_MEDICAL_CONTRACT", externalSignerRef=f"erpcontract:medical:{c.id}")
    await _sign_completed(session, p, "sign.completed", {})
    await session.flush()
    assert c.sign_status == "COMPLETED"


async def test_consent_and_other_types_ignored(session):
    """CONSENT 등 HIS 환자도메인 통지는 ERP 무관 — no-op(예외 없이 무시)."""
    c = await _medical(session)
    await _sign_completed(session, _payload("CONSENT", patientId="p1"), "sign.completed", {})
    await _sign_completed(session, _payload("SOME_HIS_TYPE"), "sign.completed", {})
    await session.flush()
    assert c.sign_status is None     # 미러 안 됨(무시)


async def test_outsourcing_and_idempotent(session):
    oc = OutsourcingContract(vendor_name="급식", category="MEAL", title="급식위탁",
                             start_on=date(2026, 1, 1), end_on=date(2026, 12, 31))
    session.add(oc)
    await session.flush()
    p = _payload("ERP_OUTSOURCING_CONTRACT", oc.id, signatureRequestId="SR-2")
    await _sign_completed(session, p, "sign.completed", {})
    await _sign_completed(session, p, "sign.completed", {})   # 멱등
    await session.flush()
    assert oc.sign_status == "COMPLETED" and oc.sign_envelope_id == "SR-2"


async def test_medical_sign_completed_activates(session):
    # 의료계약 APPROVED + 발효일 도래 → 서명완료 미러가 ACTIVE로 발효(계약통합 PR-3)
    emp = Employee(employee_no="DR2", name="의사", hired_on=date(2024, 1, 1))
    session.add(emp)
    await session.flush()
    c = MedicalStaffContract(employee_id=emp.id, valid_from=date(2026, 1, 1),
                             effective_on=date(2026, 1, 1), base_pay=5_000_000, status="APPROVED")
    session.add(c)
    await session.flush()
    await _sign_completed(session, _payload("ERP_MEDICAL_CONTRACT", c.id), "sign.completed", {})
    await session.flush()
    assert c.sign_status == "COMPLETED" and c.status == "ACTIVE"


async def test_outsourcing_sign_completed_activates(session):
    # APPROVED + 발효일 도래 → 서명완료 미러가 ACTIVE로 발효
    oc = OutsourcingContract(vendor_name="급식", category="MEAL", title="급식위탁",
                             start_on=date(2026, 1, 1), end_on=date(2026, 12, 31),
                             effective_on=date(2026, 1, 1), status="APPROVED")
    session.add(oc)
    await session.flush()
    await _sign_completed(session, _payload("ERP_OUTSOURCING_CONTRACT", oc.id), "sign.completed", {})
    await session.flush()
    assert oc.sign_status == "COMPLETED" and oc.status == "ACTIVE"


async def test_outsourcing_sign_completed_before_effective_stays(session):
    # APPROVED + 발효일 미도래 → 미러는 되되 발효는 대기(상태 유지)
    # ⚠ '미도래'는 오늘 기준으로 잡는다 — 고정 연도를 쓰면 달력이 그날을 지나는 순간
    #    전제가 뒤집혀 테스트가 조용히 거짓이 된다(2027-01-01에 깨질 예정이었음).
    later = date.today() + timedelta(days=90)
    oc = OutsourcingContract(vendor_name="급식", category="MEAL", title="급식위탁",
                             start_on=later, end_on=later + timedelta(days=365),
                             effective_on=later, status="APPROVED")
    session.add(oc)
    await session.flush()
    await _sign_completed(session, _payload("ERP_OUTSOURCING_CONTRACT", oc.id), "sign.completed", {})
    await session.flush()
    assert oc.sign_status == "COMPLETED" and oc.status == "APPROVED"


async def test_erp_type_missing_ref_raises(session):
    with pytest.raises(ValueError):     # ERP 계약 타입인데 ref 누락 → 가시화(DEAD)
        await _sign_completed(session, _payload("ERP_MEDICAL_CONTRACT"), "sign.completed", {})


async def _medical_no(session, no):
    emp = Employee(employee_no=no, name="의사", hired_on=date(2024, 1, 1))
    session.add(emp)
    await session.flush()
    c = MedicalStaffContract(employee_id=emp.id, valid_from=date(2026, 1, 1), base_pay=5_000_000)
    session.add(c)
    await session.flush()
    return c


async def test_sign_completed_bad_completed_at_no_crash(session):
    """L-1 — completedAt이 비ISO여도 예외 없이 미러(signed_at 미갱신·계약 COMPLETED 진행)."""
    c = await _medical_no(session, "DRB1")
    p = _payload("ERP_MEDICAL_CONTRACT", c.id, completedAt="not-a-date")
    await _sign_completed(session, p, "sign.completed", {})   # 종전: fromisoformat 예외→DEAD
    await session.flush()
    assert c.sign_status == "COMPLETED" and c.signed_at is None   # 파싱 실패 → 기존값(None) 유지
    # Z 접미 ISO는 정상 파싱
    c2 = await _medical_no(session, "DRB2")
    await _sign_completed(session, _payload("ERP_MEDICAL_CONTRACT", c2.id,
                                            completedAt="2026-06-28T02:05:11Z"), "sign.completed", {})
    await session.flush()
    assert c2.signed_at is not None
