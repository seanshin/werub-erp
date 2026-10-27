"""e-sign #6 ERP 의료계약 발의(origination) caller — ERP→HIS (HIS 경유 트랙).

확정 회신(seanshin/werubyHIS:docs/05-reference/his-reply-esign6-origination-final.md): 모델 (a)
ERP 주도. ERP가 계약 구조화 snapshot(JSON) 송신 → HIS 발급·PDF 렌더·documentHash·sign 제출 →
완료 시 sign.completed(erpContractId=sourceId) ERP 인박스로 자동 발신(수신·완료본 회수·미러는 LIVE).

인증: `X-Integration-Key=his_sign_origination_key`(전용 키·격리 — webhook 키 역방향 금지).
미설정 시 발의 비활성(수신·미러는 무관하게 계속 동작). 모듈 경계: hr는 integrations를 import할 수
없으므로 발의 흐름은 integrations 레이어(integrations→hr·common·audit 허용).
"""
import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from erp.integrations.egress import check_host
from erp.audit.service import AuditService
from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.config import get_settings
from erp.db import get_session
from erp.errors import ValidationError
from erp.hr.models import Employee, MedicalStaffContract

router = APIRouter(prefix="/integrations/his-sign", tags=["integrations"])
_audit = AuditService()


def _money(v) -> str | None:
    return f"월 {float(v):,.0f}원" if v is not None else None


# 봉투(envelope) 채널은 HIS DIRECT|PORTAL 2종뿐(his-reply-esign6-signer-clarify-2026-06-29).
# ERP "STAFF"(직원 SSO)는 HIS DIRECT(+requiredIdentityLevel=STAFF_SSO)에 대응 → envelope 채널은 DIRECT로
# 매핑한다. 단일 DIRECT는 HIS가 ensureStaffCert로 certSerial 주입(LIVE). 다자 signers[]는 verbatim
# 보존 — HIS prepareSigners(werubyHIS PR#117)가 STAFF 마커(signerType/requiredIdentityLevel/channel)와
# externalSignerRef(erp:staff:<empId>→Staff.erpEmployeeCode)로 per-signer certSerial을 내부 주입한다.
def _to_his_channel(ch: str | None) -> str:
    return "DIRECT" if (ch or "").strip().upper() == "STAFF" else (ch or "PORTAL")


def _build_medical_snapshot(c: MedicalStaffContract, emp: Employee) -> dict:
    """의료계약 구조화 snapshot(§4 권장 스키마). 선택 필드는 값 없으면 생략."""
    s = get_settings()
    staff = {"role": "의료진", "name": emp.name}
    if emp.position:
        staff["position"] = emp.position
    snap = {
        "title": "의료계약서",
        "contractNo": f"ERP-MC-{c.id}",
        "parties": [{"role": "병원", "name": s.org_legal_name, "org": "법인"}, staff],
        "period": {"start": str(c.valid_from),
                   "end": str(c.valid_to) if c.valid_to else None},
        "compensation": _money(c.base_pay),
        "fields": [{"label": "직군", "value": emp.job_category or emp.job_group},
                   {"label": "전속여부", "value": "전속" if c.exclusive else "비전속"}],
    }
    if c.note:
        snap["note"] = c.note
    return snap


async def request_sign(*, document_type: str, source_id, snapshot: dict,
                       channel: str = "DIRECT", signers: list | None = None) -> dict:
    """HIS 발의 엔드포인트 호출 → data(issueNo·signatureRequestId·documentHash·channel·portalUrl)."""
    s = get_settings()
    if not s.his_sign_origination_key:
        raise ValidationError("HIS 발의 키 미설정(HIS_SIGN_ORIGINATION_KEY) — 발의 비활성")
    # SSRF/오설정 가드(INV-NET-01·Phase0 감사 MED) — 페이로드에 직원 보수 포함,
    # 유일하게 화이트리스트가 없던 아웃바운드. his_billing과 동형.
    try:
        check_host("his", s.his_api_base, loopback=True)
    except ValueError as exc:
        raise ValidationError(f"허용되지 않은 발의 대상 — {exc}") from exc
    url = s.his_api_base.rstrip("/") + s.his_sign_origination_path
    body = {"documentType": document_type, "sourceId": source_id,
            "snapshot": snapshot, "channel": _to_his_channel(channel)}
    if signers:
        body["signers"] = signers
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, json=body,
                              headers={"X-Integration-Key": s.his_sign_origination_key})
        r.raise_for_status()
        return r.json()["data"]


async def initiate_medical_contract_signing(session: AsyncSession, *, actor: int,
                                            contract_id: int, channel: str = "PORTAL",
                                            signers: list | None = None,
                                            force: bool = False) -> dict:
    """의료진 계약 전자서명 개시(HIS 경유). snapshot 빌드→HIS 발의→미러 갱신(SENT).

    signers: 다자 봉투(병원장 공동/순차 등) — sign §2.5b 스키마를 HIS가 그대로 패스스루. None이면 단일.
    force: SENT 재발의 시 중복 봉투 방지 — 기본은 기존 봉투 반환(no-op), True면 새 봉투 발행.
    """
    c = await session.get(MedicalStaffContract, contract_id)
    if c is None:
        raise ValidationError("존재하지 않는 의료계약입니다")
    if c.sign_status == "COMPLETED":
        raise ValidationError("이미 서명 완료된 계약입니다")
    # 멱등 가드 — 이미 발의(SENT)된 봉투가 있으면 force 없인 새로 발행하지 않음(orphan 누적 방지).
    if c.sign_status == "SENT" and c.sign_envelope_id and not force:
        return {"contract_id": c.id, "signature_request_id": c.sign_envelope_id,
                "issue_no": c.sign_doc_ref, "channel": channel, "portal_url": None,
                "portal_links": [], "status": "SENT", "already_sent": True}
    if signers is not None and (not isinstance(signers, list)
                               or not all(isinstance(s, dict) for s in signers)):
        raise ValidationError("signers는 객체 리스트여야 합니다")
    emp = await session.get(Employee, c.employee_id)
    if emp is None:
        raise ValidationError("계약의 직원 정보를 찾을 수 없습니다")
    snapshot = _build_medical_snapshot(c, emp)
    data = await request_sign(document_type="ERP_MEDICAL_CONTRACT", source_id=c.id,
                              snapshot=snapshot, channel=channel, signers=signers)
    c.sign_envelope_id = data.get("signatureRequestId") or c.sign_envelope_id
    c.sign_status = "SENT"
    c.sign_doc_ref = data.get("issueNo") or c.sign_doc_ref   # HIS 발급번호(추적)
    await _audit.record_audit(session, actor=actor, action="ESIGN_INITIATE",
                              entity="hr.medical_contracts", target_id=str(contract_id),
                              after={"issue_no": data.get("issueNo"),
                                     "signature_request_id": data.get("signatureRequestId"),
                                     "channel": data.get("channel", channel)})
    # 서명자별 포털 deep-link(다자 PORTAL) — sign §2.5b 응답 signers[].portalUrl(HIS 패스스루).
    # 발송은 ERP 책임(D3)이므로 운영자가 각 서명자에게 전달하도록 목록으로 반환(단일은 portal_url).
    portal_links = [
        {"sign_order": s.get("signOrder"), "channel": s.get("channel"),
         "display_name": s.get("displayName"),
         "external_signer_ref": s.get("externalSignerRef"), "portal_url": s.get("portalUrl")}
        for s in (data.get("signers") or []) if s.get("portalUrl")]
    return {"contract_id": c.id, "issue_no": data.get("issueNo"),
            "signature_request_id": data.get("signatureRequestId"),
            "channel": data.get("channel", channel), "portal_url": data.get("portalUrl"),
            "portal_links": portal_links, "status": "SENT"}


class MedicalSignBody(BaseModel):
    channel: str = "PORTAL"          # PORTAL(deep-link)·STAFF/DIRECT(직원 SSO→HIS DIRECT). STAFF→DIRECT 매핑
    signers: list[dict] | None = None  # 다자 봉투(병원장 공동/순차) — 전원 PORTAL. None=단일
    force: bool = False              # SENT 재발의(중복 봉투) 허용


@router.post("/medical-contracts/{contract_id}/request", status_code=201)
async def request_medical_contract_signing(contract_id: int,
                                           body: MedicalSignBody | None = None,
                                           user: CurrentUser = Depends(get_current_user),
                                           session: AsyncSession = Depends(get_session)):
    """의료진 계약 전자서명 개시(HIS 경유) — 보수=민감, hr.payroll write.

    ERP가 snapshot(+signers) 송신 → HIS 렌더·sign 제출 → 완료 시 sign.completed로 미러·완료본 회수.
    재발의는 body.force=true(미설정 시 기존 SENT 봉투 반환).
    """
    require_scope(user, "hr.payroll", "write")
    b = body or MedicalSignBody()
    return await initiate_medical_contract_signing(
        session, actor=user.user_id, contract_id=contract_id, channel=b.channel,
        signers=b.signers, force=b.force)
