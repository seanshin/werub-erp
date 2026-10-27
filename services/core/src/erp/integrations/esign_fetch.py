"""계약 전자서명 완료본 회수 (e-sign #6 §2) — HIS 다운로드 URL+토큰 → ERP Attachment(sha256).

HIS sign.completed payload의 document.url(단기 HMAC 토큰 동봉, 24h)을 GET해 서명 완료본 PDF를
받아 ERP Attachment로 영구 보관(원본 SoT는 HIS sign, ERP는 증빙 미러). SSRF 화이트리스트·PDF 검증.
무 JWT(URL 토큰 인가) — 토큰 만료 전 1회 회수 권장.
"""
from urllib.parse import urlparse

import httpx

from erp.integrations.egress import allowed_hosts
from erp.common.attachment_service import AttachmentService
from erp.config import get_settings
from erp.errors import ValidationError

# SSRF 화이트리스트 — prod는 HIS 도메인만(https 강제). 루프백(localhost/127.0.0.1)은 dev/test 한정
# 허용(co-located prod에서 내부 포트 SSRF 방지). 리다이렉트 추종 금지로 우회 차단.
_LOOPBACK = frozenset({"localhost", "127.0.0.1"})


async def fetch_signed_doc(session, *, entity_type: str, entity_id: int, url: str,
                           issue_no: str | None = None):
    """완료본 PDF 회수 → Attachment(sha256 중복제거). 실패는 호출부가 best-effort 처리."""
    parsed = urlparse(url)
    host = parsed.hostname
    is_loopback = host in _LOOPBACK and get_settings().env != "prod"   # dev/test 한정
    if host not in allowed_hosts("his") and not is_loopback:   # SSRF 방어 — 허용 호스트만
        raise ValidationError(f"허용되지 않은 다운로드 대상: {host}")
    if parsed.scheme != "https" and not is_loopback:  # prod 도메인은 https 강제(루프백 dev만 http)
        raise ValidationError("완료본 다운로드는 https만 허용됩니다")
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        r = await client.get(url)                # URL 내 단기 HMAC 토큰으로 인가(서버↔서버)
        r.raise_for_status()
    data = r.content
    if not data[:5] == b"%PDF-":                 # 완료본 무결성 — PDF 매직바이트
        raise ValidationError("완료본이 PDF가 아닙니다")
    return await AttachmentService().save(
        session, entity_type=entity_type, entity_id=entity_id,
        filename=f"{issue_no or 'signed-contract'}.pdf", content_type="application/pdf",
        data=data, uploaded_by=None)            # 시스템 회수(인박스)


_FETCH_MAX_RETRY = 5


async def process_esign_fetch_queue(session, *, limit: int = 50) -> dict:
    """완료본 회수 아웃박스 드레인 (job_esign_fetch) — 인박스와 분리된 별도 트랜잭션에서 회수.

    PENDING + 재시도 도래한 FAILED를 FOR UPDATE SKIP LOCKED로 잡아 건별 savepoint로 회수. 이미 회수된
    계약은 DONE(멱등). 회수 실패는 선형 백오프 재시도, 한도 초과 시 FAILED(운영자 확인 — 24h 토큰 만료
    가능). 계약 미존재는 DEAD.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import and_, or_, select

    from erp.hr.models import MedicalStaffContract
    from erp.integrations.models import EsignFetchOutbox
    from erp.scm.models import OutsourcingContract

    now = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(EsignFetchOutbox).where(or_(
            EsignFetchOutbox.status == "PENDING",
            and_(EsignFetchOutbox.status == "FAILED",
                 or_(EsignFetchOutbox.next_retry_at.is_(None),
                     EsignFetchOutbox.next_retry_at <= now))))
        .order_by(EsignFetchOutbox.id).limit(limit)
        .with_for_update(skip_locked=True))).scalars().all()
    model_map = {"medical_contract": MedicalStaffContract,
                 "outsourcing_contract": OutsourcingContract}
    done = failed = 0
    for q in rows:
        try:
            async with session.begin_nested():
                model = model_map.get(q.entity_type)
                contract = await session.get(model, q.contract_id) if model else None
                if contract is None:
                    q.status, q.error = "DEAD", "계약 미존재"
                elif getattr(contract, "sign_doc_attachment_id", None):
                    q.status, q.error = "DONE", None          # 이미 회수됨(멱등)
                else:
                    att = await fetch_signed_doc(session, entity_type="contract",
                                                 entity_id=q.contract_id, url=q.doc_url,
                                                 issue_no=q.issue_no)
                    contract.sign_doc_attachment_id = att.id
                    q.status, q.error = "DONE", None
            done += 1
        except Exception as exc:  # noqa: BLE001 — 회수 실패는 재시도(미러 비차단)
            q.retry_count += 1
            q.error = str(exc)[:300]
            # 한도 도달은 DEAD — 종전엔 next_retry_at=None인 FAILED로 두었는데, 조회가 NULL도 집어
            # '최대 재시도'가 실제론 무한(2분마다 HIS 호출)이었다(v1.288 흐름검토 G10).
            if q.retry_count >= _FETCH_MAX_RETRY:
                q.status, q.next_retry_at = "DEAD", None
                # 사람이 HIS에 완료본 재발급을 요청해야 한다 — 알림 없이 DEAD면 아무도 모른다
                from erp.common.models import Notification, User
                for uid in (await session.execute(select(User.id).where(
                        User.role == "ADMIN", User.is_active.is_(True)))).scalars().all():
                    session.add(Notification(
                        user_id=uid, type="ESIGN_FETCH_DEAD", title="서명 완료본 회수 실패(재시도 종료)",
                        body=f"{q.entity_type} #{q.contract_id} 완료본을 {_FETCH_MAX_RETRY}회 받지 못했습니다 — "
                             "HIS에 완료본 재발급을 요청하세요"))
            else:
                q.status = "FAILED"
                q.next_retry_at = now + timedelta(minutes=5 * q.retry_count)
            failed += 1
    return {"done": done, "failed": failed}
