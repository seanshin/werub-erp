"""e-sign 트랙 A webhook 수신 (sign→ERP 직접) — 외부 거래처 계약 서명 완료.

확정 핸드오프 §2.5: `request.completed`, 헤더 `X-Sign-Signature: HMAC-SHA256(rawBody,
ERP_WEBHOOK_SECRET)`. ERP_WEBHOOK_SECRET은 sign이 ERP에 발급한 **소비자별 격리** 시크릿
(HIS his_webhook_key와 무관). externalDocId(`oc:<id>`)로 scm.OutsourcingContract 미러 갱신.
미설정/서명불일치는 차단. 멱등성은 종단상태(COMPLETED) 미러의 자연 멱등으로 보장(재수신 동일값).
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.config import get_settings
from erp.db import get_session
from erp.errors import ForbiddenError, ValidationError
from erp.integrations.sign_initiate import initiate_outsourcing_signing
from erp.scm.models import OutsourcingContract

logger = logging.getLogger("erp.integrations.sign_webhook")
router = APIRouter(prefix="/integrations/sign", tags=["integrations"])


def _verify(raw: bytes, header: str | None, timestamp: str | None = None) -> None:
    """sign 웹훅 HMAC 검증. 두 스킴 하위호환(L-3 조율 준비):

    - **timestamp 없음(현행)**: 서명 = HMAC(secret, rawBody). 재생 방지 없음(자연 멱등).
    - **X-Sign-Timestamp 있음(HIS 채택 시)**: 서명 = HMAC(secret, "{ts}.{rawBody}") + 신선도(skew)
      검증 → 타임스탬프가 서명에 바인딩돼 재생 창 차단(설정 콜백·인박스 동형). HIS가 ts-바인딩
      서명을 보내기 시작하면 자동으로 이 경로. 위조엔 secret 필요라 안전.
    """
    s = get_settings()
    if not s.sign_webhook_secret:
        raise ForbiddenError("sign webhook 미설정")
    sig = (header or "").removeprefix("sha256=")
    key = s.sign_webhook_secret.encode()
    if timestamp:
        try:
            skew = abs(datetime.now(timezone.utc).timestamp() - int(timestamp))
        except (TypeError, ValueError):
            raise ForbiddenError("sign webhook 타임스탬프 형식 오류") from None
        if skew > s.inbound_webhook_max_skew:
            raise ForbiddenError("sign webhook 타임스탬프 만료(재생 방지)")
        expected = hmac.new(key, f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    else:
        expected = hmac.new(key, raw, hashlib.sha256).hexdigest()
    if not sig or not hmac.compare_digest(sig, expected):
        raise ForbiddenError("sign webhook 서명 불일치")


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _contract_id(external_doc_id: str | None) -> int | None:
    """externalDocId `oc:<id>` → 계약 PK. 비-ERP/비위탁 식별자는 None(no-op)."""
    if not external_doc_id or not external_doc_id.startswith("oc:"):
        return None
    try:
        return int(external_doc_id.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


async def _apply_completed(session: AsyncSession, payload: dict) -> dict:
    cid = _contract_id(payload.get("externalDocId"))
    if cid is None:
        # 트랙A 외 식별자(직원/의료진=HIS경유 등)는 본 직접 채널 대상 아님 — no-op.
        return {"ignored": "non-outsourcing externalDocId"}
    oc = await session.get(OutsourcingContract, cid)
    if oc is None:
        raise ValidationError(f"존재하지 않는 위탁계약: {cid}")
    oc.sign_envelope_id = payload.get("requestId") or oc.sign_envelope_id
    oc.sign_status = payload.get("status") or "COMPLETED"
    sigs = payload.get("signatures") or []
    oc.signed_at = (_parse_dt(sigs[0].get("signedAt")) if sigs else None) \
        or _parse_dt(payload.get("occurredAt")) or oc.signed_at
    oc.sign_cert_serial = payload.get("signCertSerial") or oc.sign_cert_serial
    oc.sign_assurance = payload.get("assuranceLevel") or payload.get("keyCustody") \
        or oc.sign_assurance
    oc.sign_doc_ref = payload.get("auditCertUri") or oc.sign_doc_ref  # 증거(감사인증서) URI
    # 서명완료 → 발효 트리거(APPROVED/DRAFT→ACTIVE·발효일 도래 시). 미도래/상태부적합은 비차단.
    activated = await _try_activate(session, oc)
    return {"contract_id": cid, "status": oc.sign_status, "activated": activated}


async def _try_activate(session: AsyncSession, oc: OutsourcingContract) -> bool:
    """e-sign 완료 시 발효 시도 — APPROVED만(결재선 우회 차단). 발효일 미도래 등은 로그 후 skip."""
    if (oc.sign_status or "").upper() != "COMPLETED" or oc.status != "APPROVED":
        return False
    from erp.scm.contract_service import OutsourcingContractService
    try:
        await OutsourcingContractService().activate(session, actor=None, contract_id=oc.id)
        return oc.status == "ACTIVE"
    except ValidationError as exc:
        logger.info("e-sign 발효 보류", extra={"ctx": {"contract_id": oc.id, "reason": str(exc)[:120]}})   # Phase0 절단 규율
        return False  # 발효일 미도래 등 — 배치/수동 발효 대기


@router.post("/webhook", status_code=202)
async def sign_webhook(request: Request,
                       x_sign_signature: str | None = Header(default=None),
                       x_sign_timestamp: str | None = Header(default=None),
                       x_his_signature_ts: str | None = Header(default=None),
                       session: AsyncSession = Depends(get_session)):
    """sign→ERP 직접 웹훅 — HMAC(ERP_WEBHOOK_SECRET) 검증 후 위탁계약 미러 갱신.

    HIS 계약(#3·배포됨): `X-Sign-Timestamp` + `X-HIS-Signature-Ts`=HMAC("{ts}.{body}") 오면 ts-바인딩
    스킴으로 검증(재생 방지). 없으면 기존 `X-Sign-Signature`=HMAC(rawBody)(하위호환)."""
    raw = await request.body()
    ts = x_sign_timestamp if isinstance(x_sign_timestamp, str) else None    # 직접호출 시 Header 정규화
    ts_sig = x_his_signature_ts if isinstance(x_his_signature_ts, str) else None
    if ts and ts_sig:
        _verify(raw, ts_sig, ts)             # HIS ts-바인딩 스킴(X-HIS-Signature-Ts)
    else:
        _verify(raw, x_sign_signature)       # 구 rawBody 스킴(하위호환)
    # L-3(sign 웹훅 재생 방지 심화)은 nonce 거부가 아닌 X-Timestamp 바인딩이 정답 — 이 웹훅은 인박스
    # 없이 직접 적용(자연 멱등)이라 HIS의 at-least-once 재전송을 nonce로 거부하면 정상 재배달이 403으로
    # 막힘. 안전한 형태(서명 본문에 ts 포함)는 HIS 계약 변경 필요 → 조율 항목으로 보류(서명 없인 위조
    # 불가하므로 실결함 아님).
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValidationError("잘못된 JSON 본문") from None
    event_type = payload.get("eventType")
    if event_type != "request.completed":
        # 다른 생애주기 이벤트는 현재 미러 대상 아님 — 수신만 ack(인박스 적재 불요).
        logger.info("sign webhook 무시 event=%s", event_type)
        return {"received": True, "ignored": event_type}
    result = await _apply_completed(session, payload)
    # 경계 통과 감사 트레일(INV-AUDIT-01 갭 G2) — 인박스 없는 직접 적용 웹훅의 유일 기록
    from erp.audit.service import AuditService
    await AuditService().record_audit(
        session, actor=None, action="SIGN_WEBHOOK", entity="integrations.sign_webhook",
        target_id=str(payload.get("signatureRequestId") or ""), after=result)
    logger.info("sign webhook 처리 %s", result)
    return {"received": True, **result}


@router.post("/outsourcing-contracts/{contract_id}/request", status_code=201)
async def request_outsourcing_signing(contract_id: int, signer_name: str, file: UploadFile,
                                      user: CurrentUser = Depends(get_current_user),
                                      session: AsyncSession = Depends(get_session)):
    """외부 거래처 계약 전자서명 개시(트랙 A) — 계약 PDF 업로드 → 거래처 포털 deep-link 반환.

    링크는 운영자가 거래처에 전달(자동 발송 이메일/SMS은 별도 결정 — 미구현). 권한: scm.po write.
    """
    require_scope(user, "scm.po", "write")
    data = await file.read()
    return await initiate_outsourcing_signing(
        session, actor=user.user_id, contract_id=contract_id, signer_name=signer_name,
        pdf_bytes=data, filename=file.filename or f"contract-{contract_id}.pdf",
        content_type=file.content_type or "application/pdf")
