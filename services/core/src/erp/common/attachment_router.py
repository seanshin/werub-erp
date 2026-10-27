"""증빙 문서 첨부 라우터 (공통) — 전표·세금계산서·입고 등에 파일 첨부·보관.

entity_type별 권한(scope) 매핑. 업로드/삭제=write, 목록/다운로드=read.
"""
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.attachment_service import AttachmentService
from erp.common.policy import ScopeKind, require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.errors import ForbiddenError, ValidationError

router = APIRouter(prefix="/attachments", tags=["attachments"])
_svc = AttachmentService()
_audit = AuditService()

# entity_type → 권한 리소스 (해당 도메인 권한자만 그 엔터티 증빙 접근)
_ENTITY_SCOPE = {
    "tax_invoice": "finance.taxinvoice",
    "voucher": "finance.vouchers",
    "po_receipt": "scm.po",
    "contract": "hr.payroll",        # 의료진 계약 서류(보수 민감 — 계약 권한과 동일)
    "employee": "hr.employees",      # 직원 일반 서류
    "it_asset": "itam.assets",       # IT 자산 사진·증빙(ITAM)
    "itam_contract": "itam.assets",  # 벤더 계약서(ITAM)
    "software_service": "itam.assets",  # 라이선스 증서·구독 증빙(ITAM M2)
    "itam_maintenance": "itam.assets",  # 유지보수 증빙·소거 확인서(ITAM M3)
    "itam_offboarding": "itam.assets",  # 퇴사 회수 실물 사진(ITAM M4)
    "org": "admin.org",                 # 기관 로고·이미지(기관 설정)
    "expense": "finance.expenses",      # 경비 청구 영수증(Tier1 지출·경비 — 0210)
    "draft": "common.drafts",           # 기안문서 첨부(자금결재 — 0213)
    "other": "finance.statements",
}


def _scope(entity_type: str) -> str:
    resource = _ENTITY_SCOPE.get(entity_type)
    if resource is None:
        raise ValidationError(f"지원하지 않는 첨부 대상: {entity_type}")
    return resource


# entity_type → 행 수준 가드 (비-ALL scope일 때 소유권/부서 강제).
# 가드는 도메인 소유 — common은 도메인을 import할 수 없으므로(모듈경계) 도메인이 등록한다(의존성 역전).
RowGuard = Callable[[AsyncSession, CurrentUser, object, int], Awaitable[None]]
_ROW_GUARDS: dict[str, RowGuard] = {}


def register_row_guard(entity_type: str, guard: RowGuard) -> None:
    """도메인이 자신의 첨부 행 수준 가드를 등록(예: hr가 employee·contract 등록)."""
    _ROW_GUARDS[entity_type] = guard


# entity_type → 대상 존재 검증 (업로드 시 유령 entity_id에 고아 첨부 생성 방지·S-9).
# entity_type → 삭제 가드 (예: 폐기 자산의 소거 확인서 삭제·교체 차단·S-9). 둘 다 의존성 역전.
ExistenceCheck = Callable[[AsyncSession, int], Awaitable[bool]]
DeleteGuard = Callable[[AsyncSession, int], Awaitable[None]]
_EXISTENCE_CHECKS: dict[str, ExistenceCheck] = {}
_DELETE_GUARDS: dict[str, DeleteGuard] = {}


def register_existence_check(entity_type: str, check: ExistenceCheck) -> None:
    """도메인이 첨부 대상 존재 검증을 등록 — 업로드 전 entity_id 실재 확인."""
    _EXISTENCE_CHECKS[entity_type] = check


def register_delete_guard(entity_type: str, guard: DeleteGuard) -> None:
    """도메인이 첨부 삭제 가드를 등록 — 비즈니스 상태(폐기 등)로 삭제 차단(ValidationError raise)."""
    _DELETE_GUARDS[entity_type] = guard


async def _enforce_row_access(session, user: CurrentUser, scope, entity_type: str,
                              entity_id: int) -> None:
    """도메인 read/write scope가 ALL이 아니면 엔터티 소유권을 행 수준에서 강제(IDOR 차단).

    require_scope가 NONE만 막고 SELF/DEPT 종류를 폐기하던 결함 보완 — 첨부 id 열거로
    타 직원·타 부서의 계약서·보수 서류를 열람하던 경로를 닫는다.
    """
    if scope.kind == ScopeKind.ALL:
        return
    guard = _ROW_GUARDS.get(entity_type)
    if guard is None:                       # 비-ALL scope인데 가드 없음 = 안전 차단
        raise ForbiddenError()
    await guard(session, user, scope, entity_id)


@router.post("", status_code=201)
async def upload_attachment(entity_type: str, entity_id: int, file: UploadFile,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """증빙 첨부 업로드 — 디스크 sha256 저장(중복제거). 타입·용량 검증."""
    scope = require_scope(user, _scope(entity_type), "write")
    await _enforce_row_access(session, user, scope, entity_type, entity_id)
    check = _EXISTENCE_CHECKS.get(entity_type)   # S-9 — 대상 실재 확인(고아 첨부 방지)
    if check is not None and not await check(session, entity_id):
        raise ValidationError("존재하지 않는 첨부 대상입니다")
    data = await file.read()
    att = await _svc.save(session, entity_type=entity_type, entity_id=entity_id,
                          filename=file.filename or "file",
                          content_type=file.content_type or "application/octet-stream",
                          data=data, uploaded_by=user.user_id)
    await _audit.record_audit(session, actor=user.user_id, action="ATTACH_UPLOAD",
                              entity=f"attachment:{entity_type}", target_id=str(att.id),
                              after={"entity_id": entity_id, "filename": att.filename,
                                     "size": att.size_bytes})
    return {"id": att.id, "filename": att.filename, "size_bytes": att.size_bytes,
            "content_type": att.content_type}


@router.get("")
async def list_attachments(entity_type: str, entity_id: int,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """증빙 목록 — entity별."""
    scope = require_scope(user, _scope(entity_type), "read")
    await _enforce_row_access(session, user, scope, entity_type, entity_id)
    rows = await _svc.list_for(session, entity_type=entity_type, entity_id=entity_id)
    return {"items": [{"id": a.id, "filename": a.filename, "content_type": a.content_type,
                       "size_bytes": a.size_bytes, "uploaded_at": a.uploaded_at} for a in rows]}


@router.get("/{attachment_id}/download")
async def download_attachment(attachment_id: int,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """증빙 다운로드 — 저장 본문 스트리밍(Content-Type·파일명 복원)."""
    att = await _svc.get(session, attachment_id=attachment_id)
    if att is None:
        raise ValidationError("존재하지 않는 첨부입니다")
    scope = require_scope(user, _scope(att.entity_type), "read")
    await _enforce_row_access(session, user, scope, att.entity_type, att.entity_id)
    content = _svc.read_bytes(att)
    from urllib.parse import quote
    return StreamingResponse(
        iter([content]), media_type=att.content_type,
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(att.filename)}"})


@router.delete("/{attachment_id}")
async def delete_attachment(attachment_id: int,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """증빙 삭제 — 해당 도메인 write 권한. 동일내용 미참조 시 본문도 제거."""
    att = await _svc.get(session, attachment_id=attachment_id)
    if att is None:
        raise ValidationError("존재하지 않는 첨부입니다")
    scope = require_scope(user, _scope(att.entity_type), "write")
    await _enforce_row_access(session, user, scope, att.entity_type, att.entity_id)
    guard = _DELETE_GUARDS.get(att.entity_type)   # S-9 — 폐기 자산 소거확인서 등 삭제 차단
    if guard is not None:
        await guard(session, att.entity_id)
    await _audit.record_audit(session, actor=user.user_id, action="ATTACH_DELETE",
                              entity=f"attachment:{att.entity_type}", target_id=str(att.id),
                              before={"entity_id": att.entity_id, "filename": att.filename})
    await _svc.delete(session, att=att)
    return {"id": attachment_id, "deleted": True}
