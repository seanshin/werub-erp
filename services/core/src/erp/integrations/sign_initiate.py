"""e-sign 트랙 A 개시 오케스트레이션 (ERP→sign 직접) — 외부 거래처 계약 서명.

모듈 경계: scm은 integrations를 import할 수 없으므로(역류 금지) 개시 흐름은 integrations 레이어에
둔다(integrations→scm·common·audit 허용). 계약 PDF는 ERP가 Attachment로 보관(원본 미반출),
sign엔 SHA-256만 송신. enroll(WITNESS)→requests(PORTAL·경량NONE)→portal-tokens.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.attachment_service import AttachmentService
from erp.config import get_settings
from erp.errors import ValidationError
from erp.integrations import sign_client
from erp.scm.models import OutsourcingContract

_audit = AuditService()


def _links(tokens: list[dict]) -> list[dict]:
    return [{"signer_role": t.get("signerRole"), "link": t.get("link")} for t in tokens]


async def initiate_outsourcing_signing(session: AsyncSession, *, actor: int,
                                       contract_id: int, signer_name: str, pdf_bytes: bytes,
                                       filename: str, content_type: str) -> dict:
    """외부 거래처 계약 전자서명 개시. 거래처 발송용 포털 deep-link 반환.

    이미 개시(미완료)된 경우 인증서/요청 재생성 없이 포털 토큰만 재발행(중복 방지·재전송).
    """
    c = await session.get(OutsourcingContract, contract_id)
    if c is None:
        raise ValidationError("존재하지 않는 계약입니다")
    if c.sign_status == "COMPLETED":
        raise ValidationError("이미 서명 완료된 계약입니다")
    if content_type != "application/pdf":
        raise ValidationError("계약 원본은 PDF만 허용됩니다")

    # 미완료 개시건 → 토큰만 재발행(재전송용).
    if c.sign_envelope_id and c.sign_status in ("SENT", "PENDING"):
        tokens = await sign_client.portal_tokens(c.sign_envelope_id)
        return {"contract_id": c.id, "request_id": c.sign_envelope_id,
                "status": c.sign_status, "resent": True, "links": _links(tokens)}

    # 계약 PDF를 ERP 증빙으로 보관(sha256 중복제거) → documentHash 산출.
    att = await AttachmentService().save(
        session, entity_type="contract", entity_id=contract_id,
        filename=filename or f"contract-{contract_id}.pdf",
        content_type=content_type, data=pdf_bytes, uploaded_by=actor)
    document_hash = att.sha256

    cert_serial = await sign_client.enroll_witness(signer_name)
    request_id = await sign_client.create_portal_request(
        idempotency_key=f"oc:{contract_id}:sign", external_doc_id=f"oc:{contract_id}",
        title=c.title, document_hash=document_hash,
        callback_url=get_settings().sign_callback_url,
        signer_name=signer_name, cert_serial=cert_serial)
    tokens = await sign_client.portal_tokens(request_id)

    c.sign_envelope_id = request_id
    c.sign_status = "SENT"
    c.sign_cert_serial = cert_serial
    c.sign_doc_ref = f"sha256:{document_hash}"   # 송신한 문서 해시(증거)
    c.sign_doc_attachment_id = att.id            # ERP 보관 원본(완료본 아님)
    await _audit.record_audit(session, actor=actor, action="ESIGN_INITIATE",
                              entity="scm.outsourcing_contracts", target_id=str(contract_id),
                              after={"request_id": request_id, "signer": signer_name,
                                     "document_hash": document_hash})
    return {"contract_id": c.id, "request_id": request_id, "status": "SENT",
            "document_hash": document_hash, "links": _links(tokens)}
