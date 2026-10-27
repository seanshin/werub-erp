"""관리자 라우터 — 부서 관리·감사/접속기록 조회 (design §4.3)."""
from datetime import date, datetime, time, timedelta
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.models import AccessLog, AuditLog
from erp.audit.service import AuditService
from erp.common.models import Department
from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.errors import ValidationError
from erp.common.months import YearMonth
from erp.common.kst import as_kst_wallclock, kst_iso, kst_now

# HIS 인바운드 이벤트별 **기대 수신 주기(시간)** — 무수신 경보 임계.
# 등재된 것만 경보하며, 미등재 이벤트는 경보하지 않고 최근 수신 시각만 표시한다.
# 근거: HIS 회신 2026-07-28(`docs/01-plan/integrations/his-inbound-liveness-request-2026-07-28.md`)
#   — 무수신 6종은 전부 **업무 활동이 있을 때만 발생**하는 이벤트(저빈도가 정상).
# 계약으로 주기가 확정되는 이벤트가 생기면 여기에 추가한다.
INBOUND_EXPECTED_HOURS: dict[str, int] = {}

router = APIRouter(tags=["admin"])
_audit = AuditService()


def _require(user: CurrentUser, resource: str, action: str):
    """단일 권한 게이트 위임 (M5 수렴)."""
    return require_scope(user, resource, action)


# ── 부서 관리 ──────────────────────────────────────────────
class DepartmentBody(BaseModel):
    name: str
    parent_id: int | None = None
    head_user_id: int | None = None


class HolidayBody(BaseModel):
    on_date: date
    name: str
    kind: str = "PUBLIC"


# ── 공휴일 캘린더(P3) — 변동 공휴일 관리(org-config 권한) ─────────────
@router.get("/holidays")
async def list_holidays(year: int | None = None,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "common.departments", "read")
    from erp.common.holidays import HolidayService
    return {"items": await HolidayService().list(session, year=year)}


@router.post("/holidays", status_code=201)
async def add_holiday(body: HolidayBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    _require(user, "common.departments", "write")
    from erp.common.holidays import HolidayService
    h = await HolidayService().add(session, on_date=body.on_date, name=body.name,
                                   kind=body.kind, actor=user.user_id)
    return {"id": h.id, "on_date": h.on_date.isoformat(), "name": h.name}


@router.get("/holidays/candidates")
async def holiday_candidates(year: int, user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """그 해 공휴일 후보(원문 대조용)·등록 여부·검토 기록 — 2026 노동절·제헌절·선거일, 2027 음력·대체공휴일."""
    _require(user, "common.departments", "read")
    from erp.common.holidays import candidates
    return await candidates(session, year=year)


class HolidayReviewBody(BaseModel):
    dates: list[date]                        # 등록할 후보 날짜(나머지 후보는 '뺀 날짜'로 기록)
    source_note: str


@router.post("/holidays/candidates/{year}/review")
async def review_holiday_candidates(year: int, body: HolidayReviewBody,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """후보 검토 확정 — 고른 날짜 등록·나머지는 '뺀 날짜'로 기록(근거 필수·감사). 결정·조치함의 그 해 항목이 닫힌다."""
    _require(user, "common.departments", "write")
    from erp.common.holidays import review_candidates
    return await review_candidates(session, year=year, register=body.dates, source_note=body.source_note,
                                   actor=user.user_id)


@router.delete("/holidays/{holiday_id}")
async def delete_holiday(holiday_id: int,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _require(user, "common.departments", "write")
    from erp.common.holidays import HolidayService
    if not await HolidayService().delete(session, holiday_id=holiday_id):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="공휴일 기록이 없습니다")
    return {"ok": True}


@router.get("/departments")
async def list_departments(user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    _require(user, "common.departments", "read")   # 인가 게이트(조직구조 열람 — 매니저+)
    rows = (await session.execute(
        select(Department).order_by(Department.id).limit(1000))).scalars().all()  # 방어적 상한
    return {"items": [{"id": d.id, "name": d.name, "code": d.code, "parent_id": d.parent_id,
                       "head_user_id": d.head_user_id} for d in rows]}


@router.post("/departments", status_code=201)
async def create_department(body: DepartmentBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    _require(user, "common.departments", "write")  # 조직 관리 정책 키 (Gap-6 정정)
    dept = Department(name=body.name, parent_id=body.parent_id, head_user_id=body.head_user_id)
    session.add(dept)
    await session.flush()
    await _audit.record_audit(session, actor=user.user_id, action="CREATE",
                              entity="common.departments", target_id=str(dept.id),
                              after=body.model_dump())
    return {"id": dept.id}


@router.patch("/departments/{dept_id}")
async def update_department(dept_id: int, body: DepartmentBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    _require(user, "common.departments", "write")
    dept = (await session.execute(
        select(Department).where(Department.id == dept_id))).scalar_one_or_none()
    if dept is None:
        raise ValidationError("존재하지 않는 부서입니다")
    before = {"name": dept.name, "parent_id": dept.parent_id, "head_user_id": dept.head_user_id}
    dept.name, dept.parent_id, dept.head_user_id = body.name, body.parent_id, body.head_user_id
    await _audit.record_audit(session, actor=user.user_id, action="UPDATE",
                              entity="common.departments", target_id=str(dept_id),
                              before=before, after=body.model_dump())
    return {"ok": True}


# ── 스테이징 재처리 (design §5.2-3, E2E ⑥) ────────────────
def _staging_kind(kind: str):
    """재처리 대상 staging 디스패치 — (모델, 재처리 상태, entity명).

    notice는 파서가 REPROCESSED를 재소비. 나머지 ACL staging은 소비 잡이 NEW를
    재소비하므로 FAILED→NEW로 되돌린다 (CHECK: NEW/APPLIED/FAILED)."""
    from erp.claims.models import NoticeFileStaging
    from erp.hr.models import GroupwareAttendanceStaging
    from erp.patient.models import HISPatientStaging, HISPaymentStaging

    table = {
        "notice": (NoticeFileStaging, "REPROCESSED", "integration.hira_notice_staging"),
        "attendance": (GroupwareAttendanceStaging, "NEW",
                       "integration.groupware_attendance_staging"),
        "his_patient": (HISPatientStaging, "NEW", "integration.his_patient_staging"),
        "his_payment": (HISPaymentStaging, "NEW", "integration.his_payment_staging"),
    }
    if kind not in table:
        raise ValidationError("알 수 없는 staging 종류입니다",
                              {"kind": kind, "allowed": list(table)})
    return table[kind]


@router.post("/admin/staging/{staging_id}/reprocess")
async def reprocess_staging(staging_id: int, kind: str = "notice",
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """실패 행 재처리 — FAILED만 허용, audit 기록. 다운스트림은 source_ref 멱등.

    kind: notice(기본) | attendance | his_patient | his_payment."""
    _require(user, "admin.staging", "write")
    model, reset_status, entity = _staging_kind(kind)
    staging = (await session.execute(
        select(model).where(model.id == staging_id)
    )).scalar_one_or_none()
    if staging is None:
        raise ValidationError("존재하지 않는 스테이징입니다")
    if staging.status != "FAILED":
        raise ValidationError("FAILED 상태만 재처리할 수 있습니다", {"status": staging.status})
    staging.status = reset_status
    staging.error_detail = None
    await _audit.record_audit(session, actor=user.user_id, action="REPROCESS",
                              entity=entity, target_id=str(staging_id))
    return {"id": staging_id, "kind": kind, "status": staging.status}


@router.get("/admin/staging/failed")
async def list_failed_staging(kind: str = "notice", limit: int = 100,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """FAILED staging 가시화 (M-2) — 운영자가 사유 확인 후 reprocess 또는 amend로 라우팅.

    예: kind=his_payment & error_detail.reason=day_closed → FR-54 정정(amend) 대상."""
    _require(user, "admin.staging", "read")
    model, _, entity = _staging_kind(kind)
    rows = (await session.scalars(
        select(model).where(model.status == "FAILED")
        .order_by(model.id.desc()).limit(min(limit, 500)))).all()

    def _ref(r):
        return getattr(r, "source_ref", None) or getattr(r, "original_name", None)

    return {"kind": kind, "entity": entity, "items": [
        {"id": r.id, "ref": _ref(r), "error_detail": r.error_detail,
         "received_at": kst_iso(r.received_at)}
        for r in rows]}


# ── ERP 마스터코드 회신 (HIS 연동) ─────────────────────────
@router.get("/admin/master-codes")
async def list_master_codes(kind: str = "item", active_only: bool = True, limit: int = 2000,
                            after: str | None = None,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """ERP 마스터코드 회신 — HIS가 자기 코드↔erp*Code 매핑 저장용.

    kind: item(scm.items.code) | asset(finance.fixed_assets.asset_no) | employee(hr.employees.employee_no).
    자산·직원은 HIS 발번 식별자를 ERP가 그대로 사용(**1:1 항등** — mapping=IDENTITY), 품목은 ERP 자체코드.
    truncated=True면 next_after(마지막 code) 이후로 재요청해 전량 페이지네이션(keyset).
    """
    _require(user, "admin.staging", "read")
    # 상한 도달 시 truncated=True 신호 — 침묵 절단 시 상한 밖 코드가 HIS에서 영구 미매핑되는 것 방지.
    # (품목 카탈로그는 5000 SKU 초과 가능; 초과 시 HIS가 마지막 code 이후로 재요청 → after 커서.)
    limit = min(limit, 5000)
    if kind == "item":
        from erp.scm.models import Item
        q = select(Item.code, Item.name, Item.category, Item.barcode, Item.is_narcotic)
        if after:
            q = q.where(Item.code > after)
        rows = (await session.execute(q.order_by(Item.code).limit(limit))).all()
        items = [{"erp_item_code": c, "name": n, "category": cat, "barcode": bc,
                  "is_narcotic": nar} for c, n, cat, bc, nar in rows]
        return {"kind": "item", "mapping": "ERP_OWN_CODE", "count": len(items),
                "truncated": len(items) == limit,
                "next_after": items[-1]["erp_item_code"] if len(items) == limit else None,
                "items": items}
    if kind == "asset":
        from erp.finance.models import FixedAsset
        q = select(FixedAsset.asset_no, FixedAsset.name, FixedAsset.status)
        if active_only:
            q = q.where(FixedAsset.status.notin_(["DISPOSED", "RETIRED"]))
        if after:
            q = q.where(FixedAsset.asset_no > after)
        rows = (await session.execute(q.order_by(FixedAsset.asset_no).limit(limit))).all()
        items = [{"erp_asset_code": a, "name": n, "status": s} for a, n, s in rows]
        return {"kind": "asset", "mapping": "IDENTITY", "count": len(items),
                "truncated": len(items) == limit,
                "next_after": items[-1]["erp_asset_code"] if len(items) == limit else None,
                "items": items}
    if kind == "employee":
        from erp.hr.models import Employee
        q = select(Employee.employee_no, Employee.name, Employee.external_user_id,
                   Employee.source, Employee.resigned_on)
        if active_only:
            q = q.where(Employee.resigned_on.is_(None))
        if after:
            q = q.where(Employee.employee_no > after)
        rows = (await session.execute(q.order_by(Employee.employee_no).limit(limit))).all()
        items = [{"erp_employee_code": e, "name": n, "external_user_id": x, "source": src,
                  "active": r is None} for e, n, x, src, r in rows]
        return {"kind": "employee", "mapping": "IDENTITY", "count": len(items),
                "truncated": len(items) == limit,
                "next_after": items[-1]["erp_employee_code"] if len(items) == limit else None,
                "items": items}
    raise ValidationError("kind는 item|asset|employee")


# ── 감사·접속기록 조회 (원장·관리자 한정 — NFR Audit) ──────
def _parse_day(s: str | None):
    """YYYY-MM-DD → date(None이면 무시, 형식 오류는 무시)."""
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _log_filters(model, *, actor, action, entity, target_id, date_from, date_to,
                 route=None):
    """공통 로그 필터 절 — 행위자/동작(정확)/대상·엔티티/대상ID/경로(부분)/기간."""
    conds = []
    if actor is not None:
        conds.append(model.actor == actor)
    if action:
        conds.append(model.action == action)
    if entity:
        conds.append(model.entity.ilike(f"%{entity}%"))
    if target_id:
        conds.append(model.target_id.ilike(f"%{target_id}%"))
    if route is not None and hasattr(model, "route"):
        conds.append(model.route.ilike(f"%{route}%"))
    df = _parse_day(date_from)
    if df is not None:
        conds.append(model.at >= datetime.combine(df, time.min))
    dt_ = _parse_day(date_to)
    if dt_ is not None:                       # 종료일 포함(다음날 0시 미만)
        conds.append(model.at < datetime.combine(dt_ + timedelta(days=1), time.min))
    return conds


@router.get("/admin/audit-logs")
async def list_audit_logs(user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session),
                          page: int = 1, size: int = 50,
                          actor: int | None = None, action: str | None = None,
                          entity: str | None = None, target_id: str | None = None,
                          date_from: str | None = None, date_to: str | None = None):
    """감사로그 목록 — 변경 전/후 스냅샷 포함(민감값 서버 마스킹).

    `before`/`after`가 없으면 감사로그는 "누가 언제 무엇을 만졌다"까지만 말하고 **무엇이 어떻게
    바뀌었는지**는 알 수 없어 감사 목적을 절반만 충족한다. 다만 이 스냅샷에는 도메인별로
    계좌번호·급여·부양가족이 평문으로 들어 있어(실측: `account_no`·`base_pay`·`dependents`),
    그대로 내보내면 **화면에서 마스킹하던 값을 감사로그 경유로 우회 노출**하게 된다.
    → `mask_audit_payload`로 키 이름·값 모양 양쪽 판정해 가린 뒤 반환한다.
    행위자는 이름으로 해소한다(내부 PK만으로는 책임 소재가 안 보인다).
    """
    _require(user, "admin.audit", "read")
    from erp.common.crypto import mask_audit_payload
    from erp.hr.employee_service import user_labels
    size = min(max(size, 1), 200)
    conds = _log_filters(AuditLog, actor=actor, action=action, entity=entity,
                         target_id=target_id, date_from=date_from, date_to=date_to)
    total = await session.scalar(
        select(func.count()).select_from(AuditLog).where(*conds)) or 0
    rows = (await session.execute(
        select(AuditLog).where(*conds).order_by(AuditLog.at.desc())
        .offset((page - 1) * size).limit(size))).scalars().all()
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="audit.audit_log", route="/admin/audit-logs")
    labels = await user_labels(session, [r.actor for r in rows])
    return {"items": [{"id": r.id, "actor": r.actor, "actor_name": labels.get(r.actor),
                       "action": r.action, "entity": r.entity,
                       "target_id": r.target_id, "at": r.at,
                       "before": mask_audit_payload(r.before),
                       "after": mask_audit_payload(r.after)} for r in rows],
            "count": total, "page": page, "size": size, "masked": True}


@router.get("/admin/access-logs")
async def list_access_logs(user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session),
                           page: int = 1, size: int = 50,
                           actor: int | None = None, action: str | None = None,
                           entity: str | None = None, target_id: str | None = None,
                           route: str | None = None,
                           date_from: str | None = None, date_to: str | None = None):
    _require(user, "admin.audit", "read")
    size = min(max(size, 1), 200)
    conds = _log_filters(AccessLog, actor=actor, action=action, entity=entity,
                         target_id=target_id, date_from=date_from, date_to=date_to,
                         route=route)
    total = await session.scalar(
        select(func.count()).select_from(AccessLog).where(*conds)) or 0
    rows = (await session.execute(
        select(AccessLog).where(*conds).order_by(AccessLog.at.desc())
        .offset((page - 1) * size).limit(size))).scalars().all()
    # 분석 M6 — 접속기록 열람 자체도 기록
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="audit.access_log", route="/admin/access-logs")
    return {"items": [{"id": r.id, "actor": r.actor, "action": r.action, "entity": r.entity,
                       "target_id": r.target_id, "route": r.route, "at": r.at} for r in rows],
            "count": total, "page": page, "size": size}


@router.get("/admin/integrations/status")
async def integration_status(user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """연동 상태 모니터 (관리자) — 설정 여부·타입프리픽스(시크릿 값 비노출)·인박스 요약."""
    from sqlalchemy import func

    from erp.config import get_settings
    from erp.hr.models import LeaveRequest
    from erp.integrations.models import HisInboundEvent

    _require(user, "admin.audit", "read")
    s = get_settings()
    from erp.common.org_service import sso_policy
    sso = await sso_policy(session)
    from erp.integrations.egress import allowed_hosts

    def pfx(v: str | None) -> str | None:    # 타입 프리픽스만(예: clinic_·erpik_) — 시크릿 비노출
        return f"{v[:8]}…(len {len(v)})" if v else None

    integrations = [
        {"name": "HIS SSO", "env": "HIS_JWT_SECRET", "configured": bool(s.his_jwt_secret),
         "detail": f"aud={sso['audience'] or '-'} · token방식 "
                   f"{'허용' if sso['token_allowed'] else '차단'}"
                   + (" · ⚠정책 미결정" if sso["undecided"] else "")},
        {"name": "Clinic 직원동기화", "env": "CLINIC_API_KEY", "configured": bool(s.clinic_api_key),
         "detail": pfx(s.clinic_api_key)},
        {"name": "HIS 환자·수납", "env": "HIS_API_KEY", "configured": bool(s.his_api_key),
         "detail": pfx(s.his_api_key)},
        {"name": "공공 수가", "env": "FEE_API_SERVICE_KEY", "configured": bool(s.fee_api_service_key)},
        {"name": "전자결재 콜백", "env": "EAPPROVAL_CALLBACK_SECRET",
         "configured": bool(s.eapproval_callback_secret), "detail": "HMAC 검증"},
        {"name": "전자결재 상신", "env": "HIS_INTEGRATION_KEY",
         "configured": bool(s.his_integration_key), "enabled": s.eapproval_submit_enabled,
         "detail": pfx(s.his_integration_key)},
        {"name": "웹훅 게이트웨이", "env": "HIS_WEBHOOK_KEY", "configured": bool(s.his_webhook_key)},
        {"name": "백업/DR", "env": "BACKUP_DIR", "configured": bool(s.backup_dir)},
    ]
    # 건수만 반환하면 **연동이 며칠째 멎어도 APPLIED 숫자가 그대로라 정상으로 보인다**
    # (v1.236/1.237의 DEAD 사고 유형). 이벤트별 마지막 수신 시각과 최근 오류를 함께 낸다.
    # stale 판정은 서버가 한다 — 클라이언트 시계에 의존하지 않고 임계를 한 곳에 둔다.
    # 되읽기 값 산술은 as_kst_wallclock로 정규화(PG=UTC aware·sqlite=naive 혼재 방지·저장소 규율).
    _now = as_kst_wallclock(kst_now())

    def _stale(st: str, event_type: str, last) -> bool:
        """기대 주기가 **계약으로 정해진 이벤트만** 무수신 경보 대상.

        ⚠ 초기 구현은 24h 일괄이었으나 오탐이었다 — HIS 회신(2026-07-28)으로 무수신 6종이 전부
        "정상(저빈도)"이고 **업무 활동이 있을 때만 발생**하는 이벤트임이 확인됐다(HIS 마지막 발송일과
        ERP 마지막 수신일이 전건 일치). 활동 기반 이벤트에 고정 임계를 걸면 조용한 기간마다 적색이 떠
        모니터가 무의미해진다(정기업무 health_signal의 영구 발화와 같은 실패 유형).
        → 여기 등재된 이벤트만 경보하고, 나머지는 **최근 수신 시각만 표시**해 사람이 판단하게 한다.
        """
        hours = INBOUND_EXPECTED_HOURS.get(event_type)
        if hours is None or st != "APPLIED":
            return False
        w = as_kst_wallclock(last)
        return bool(w is not None and (_now - w).total_seconds() > hours * 3600)

    inbox = [{"domain": d, "event_type": et, "status": st, "count": c,
              "last_received_at": kst_iso(last),
              "expected_hours": INBOUND_EXPECTED_HOURS.get(et),
              "stale": _stale(st, et, last)}
             for d, et, st, c, last in (await session.execute(
                 select(HisInboundEvent.domain, HisInboundEvent.event_type,
                        HisInboundEvent.status, func.count(),
                        func.max(HisInboundEvent.received_at))
                 .group_by(HisInboundEvent.domain, HisInboundEvent.event_type,
                           HisInboundEvent.status))).all()]
    inbox.sort(key=lambda r: (r["domain"], r["event_type"], r["status"]))
    # 실패 사유 — 건수만 보이고 원인이 없어 화면에서 원인 판별이 불가했다(최근 5건·절단 준수)
    inbox_errors = [{"domain": d, "status": st, "error": (err or "")[:120],
                     "received_at": kst_iso(at)}
                    for d, st, err, at in (await session.execute(
                        select(HisInboundEvent.domain, HisInboundEvent.status,
                               HisInboundEvent.error, HisInboundEvent.received_at)
                        .where(HisInboundEvent.status.in_(("FAILED", "DEAD")))
                        .order_by(HisInboundEvent.received_at.desc()).limit(5))).all()]
    linked = await session.scalar(
        select(func.count()).select_from(LeaveRequest).where(LeaveRequest.approval_id.isnot(None)))
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="admin.integrations", route="/admin/integrations/status")
    return {"integrations": integrations, "inbox": inbox,
            "inbox_errors": inbox_errors,
            "eapproval_linked_leaves": int(linked or 0),
            # 아웃바운드 허용 호스트(실효값) — 호출이 '허용되지 않은 대상'으로 막힐 때 화면에서 원인을
            # 보게 한다. 추가는 서버 설정 *_ALLOWED_HOSTS(INV-NET-01: base URL과 별도로 적는다).
            "egress": {name: sorted(allowed_hosts(name)) for name in ("his", "clinic", "ai")}}


@router.get("/admin/diagnostics/db-connections")
async def db_connection_diagnostics(user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """공유 PG 연결 슬롯 진단 (관리자, 읽기전용) — ERP 자체 풀 + 테넌트별 연결수.

    공유 박스(HIS·OpenPACS 동거, max_connections=200)에서 OpenPACS 유휴 누수로 슬롯
    고갈→HIS SSO 500 반복 인시던트의 **조기경보**. usename별 연결수는 보이나, 타 테넌트의
    state/idle-age는 erp_app 비특권이라 가려짐(NULL). 회복(openpacs idle>60m 종료)은
    superuser·HIS팀 영역 — 진단/회복 SoT는 ops-runbook §9.1. sqlite(단위테스트)는 shared_pg=null."""
    from erp.app.db_diagnostics_service import shared_pg_stats
    from erp.db import get_engine

    _require(user, "admin.audit", "read")

    pool = get_engine().pool

    def _stat(name: str) -> int | None:        # 풀 타입별 메서드 차이 방어(StaticPool 등)
        fn = getattr(pool, name, None)
        try:
            return fn() if callable(fn) else None
        except Exception:
            return None

    erp_pool = {"size": _stat("size"), "checked_out": _stat("checkedout"),
                "overflow": _stat("overflow"), "configured": "pool 10 + overflow 5"}

    shared_pg = await shared_pg_stats(session)      # P2-4 공유 통계(잡과 동일 소스)
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="admin.diagnostics", route="/admin/diagnostics/db-connections")
    return {"erp_pool": erp_pool, "shared_pg": shared_pg}


@router.post("/admin/integrations/billing/backfill")
async def billing_backfill(from_on: date, to_on: date,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """HIS 진료라인 백필 pull (D-6) — 운영 전환 시 기존 발생분 일괄 적재(멱등)."""
    from erp.integrations.his_billing import backfill_lines
    _require(user, "admin.staging", "write")   # GL/마스터 변경 → write 게이트(read 금지·전체검토)
    out = await backfill_lines(session, from_on=from_on, to_on=to_on)
    await _audit.record_access(session, actor=user.user_id, action="UPDATE",
                               entity="patient.billing_lines",
                               route="/admin/integrations/billing/backfill")
    return out


@router.post("/admin/integrations/esign-emp-map")
async def esign_emp_map_backfill(signatories_only: bool = True, dry_run: bool = False,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """e-sign 사번 매핑 백필 — HIS 디렉터리 조인 → map EP로 erpEmployeeCode 채움(HIS (나) 확정).

    signatories_only=의료계약 서명자 우선·dry_run=조인만(발신 없음). 관리자."""
    from erp.integrations.esign_emp_map import backfill
    _require(user, "admin.staging", "write")
    out = await backfill(session, actor=user.user_id, signatories_only=signatories_only,
                         dry_run=dry_run)
    await _audit.record_access(session, actor=user.user_id, action="UPDATE",
                               entity="integrations.esign_emp_map",
                               route="/admin/integrations/esign-emp-map")
    return out


@router.get("/admin/integrations/patient-ref/status")
async def patient_ref_status(user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """Phase0 가명 토큰 대사 현황 — 이관 건수·미등록 보류 큐·키 지문·컷오버 플립 상태(관리자).

    HIS 컷오버 대비 모니터: total_mapped(대사표 이관 완료 수)·pending(신규환자 미등록)·
    key_configured(키 주입 여부)·cutover_enabled(his_pseudonym_enabled). 키 원문·지문은 비노출."""
    from erp.config import get_settings
    from erp.common.org_service import OrgSettingsService
    from erp.integrations.models import PatientRefMap, PendingPatientRef
    _require(user, "admin.staging", "read")
    s = get_settings()
    mapped = int((await session.scalar(select(func.count()).select_from(PatientRefMap))) or 0)
    pending = int((await session.scalar(
        select(func.count()).select_from(PendingPatientRef).where(
            PendingPatientRef.resolved_chart_no.is_(None)))) or 0)
    db_flag = await OrgSettingsService().get_phase0_cutover(session)
    return {"total_mapped": mapped, "pending_unresolved": pending,
            "key_configured": bool(s.pseudonym_key),
            # env(레거시·상시) 또는 DB(관리화면 토글) 중 하나라도 ON=활성
            "cutover_enabled": bool(s.his_pseudonym_enabled) or db_flag,
            "cutover_env": bool(s.his_pseudonym_enabled), "cutover_runtime": db_flag}


@router.get("/admin/integrations/patient-ref/pending")
async def patient_ref_pending(limit: int = 200,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """미등록 patientRef 보류 큐 — 컷오버 후 신규환자(대사표 미등록) 수신분. ⚠토큰만(원 MRN 없음)."""
    from erp.integrations.models import PendingPatientRef
    _require(user, "admin.staging", "read")
    rows = (await session.scalars(select(PendingPatientRef).where(
        PendingPatientRef.resolved_chart_no.is_(None))
        .order_by(PendingPatientRef.event_count.desc()).limit(min(limit, 500)))).all()
    return {"items": [{"id": r.id, "patient_ref": r.patient_ref, "first_event": r.first_event,
                       "event_count": r.event_count,
                       "first_seen": kst_iso(r.first_seen),
                       "last_seen": r.last_seen.isoformat() if r.last_seen else None}
                      for r in rows]}


class CutoverBody(BaseModel):
    enabled: bool


@router.post("/admin/integrations/patient-ref/cutover")
async def patient_ref_cutover(body: CutoverBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """Phase0 컷오버 게이트 토글(무재시작) — ADMIN. ⚠보안·법무 재식별위험 확인 선행(사람 결정).

    ON=inbound consumer가 patientRef 역해소 시작(원 MRN egress 제거). OFF=즉시 롤백(원 chartNo 통과).
    HIS pseudonymizeChartNo 플립과 합의 일시 동시 수행. 감사 기록."""
    from erp.common.org_service import OrgSettingsService
    _require(user, "admin.staging", "write")
    flag = await OrgSettingsService().set_phase0_cutover(session, enabled=body.enabled,
                                                         actor=user.user_id)
    await _audit.record_access(session, actor=user.user_id, action="UPDATE",
                               entity="integrations.phase0_cutover",
                               route="/admin/integrations/patient-ref/cutover")
    await session.commit()
    return {"cutover_runtime": flag}


class SsoPolicyBody(BaseModel):
    token_mode: str | None = None          # "allow" | "block" | None(변경 안 함)
    enforce_audience: bool | None = None


@router.get("/admin/integrations/sso-policy")
async def get_sso_policy(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """HIS SSO 실효 정책 — token 방식 허용·aud 강제·미결정 항목(관리자 결정 화면용)."""
    from erp.common.org_service import sso_policy
    _require(user, "admin.org", "read")
    return await sso_policy(session)


@router.put("/admin/integrations/sso-policy")
async def put_sso_policy(body: SsoPolicyBody,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """HIS SSO 정책 결정(무재시작) — ADMIN. 감사 기록.

    ⚠ token 차단·aud 강제는 HIS가 cookie 방식(erp.handoff='cookie')으로 핸드오프해야 로그인이
    유지된다 — HIS 설정과 함께 바꿀 것(화면 안내)."""
    from erp.common.org_service import set_sso_policy
    _require(user, "admin.org", "write")
    out = await set_sso_policy(session, token_mode=body.token_mode,
                               enforce_audience=body.enforce_audience, actor=user.user_id)
    await _audit.record_access(session, actor=user.user_id, action="UPDATE",
                               entity="common.org_settings.sso_policy",
                               route="/admin/integrations/sso-policy")
    await session.commit()
    return out


@router.post("/admin/integrations/billing/unbilled-progress")
async def billing_unbilled_progress(as_of: date,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """HIS 미청구 진행분 pull → 미청구수익 발생(K-IFRS 1115 B-1, 멱등). 결산 시 실행."""
    from erp.integrations.his_billing import pull_unbilled_progress
    _require(user, "admin.staging", "write")   # GL/마스터 변경 → write 게이트(read 금지·전체검토)
    out = await pull_unbilled_progress(session, as_of=as_of)
    await _audit.record_access(session, actor=user.user_id, action="UPDATE",
                               entity="finance.unbilled_revenue",
                               route="/admin/integrations/billing/unbilled-progress")
    await session.commit()
    return out


class BillingGradeBody(BaseModel):
    grade: str                       # CLINIC|HOSPITAL|GENERAL|TERTIARY
    outpatient_rate: float           # 0.30~0.60
    effective_from: date
    inpatient_rate: float = 0.20
    meal_rate: float = 0.50
    special_case_rate: float = 0.05
    note: str | None = None


@router.get("/admin/billing/grade")
async def billing_grade_list(user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """기관 종별 본인부담률 설정 — 현재 + 변경 이력(시점관리)."""
    from erp.patient.billing_engine import GRADE_LABEL
    from erp.patient.billing_rates import current_grade
    from erp.regulatory.models import InstitutionGradeSetting
    _require(user, "regulatory.master", "read")
    rows = (await session.scalars(select(InstitutionGradeSetting)
            .order_by(InstitutionGradeSetting.effective_from.desc()).limit(500))).all()
    cur = await current_grade(session)
    return {"current": {**cur, "label": GRADE_LABEL.get(cur["grade"])} if cur else None,
            "history": [{"id": r.id, "grade": r.grade, "label": GRADE_LABEL.get(r.grade),
                         "outpatient_rate": float(r.outpatient_rate),
                         "inpatient_rate": float(r.inpatient_rate),
                         "effective_from": r.effective_from, "note": r.note}
                        for r in rows]}


@router.post("/admin/billing/grade", status_code=201)
async def billing_grade_set(body: BillingGradeBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """기관 종별 변경 — 신규 행(effective_from). 과거 진료는 당시 종별로 계산 유지."""
    from erp.regulatory.models import InstitutionGradeSetting
    _require(user, "regulatory.master", "write")
    if body.grade not in ("CLINIC", "HOSPITAL", "GENERAL", "TERTIARY"):
        raise ValidationError("종별은 CLINIC/HOSPITAL/GENERAL/TERTIARY")
    if not 0 < body.outpatient_rate <= 1:
        raise ValidationError("외래 본인부담률은 0~1")
    dup = await session.scalar(select(InstitutionGradeSetting).where(
        InstitutionGradeSetting.effective_from == body.effective_from))
    if dup is not None:
        raise ValidationError(f"{body.effective_from} 시행 설정이 이미 있습니다")
    row = InstitutionGradeSetting(
        grade=body.grade, outpatient_rate=body.outpatient_rate,
        inpatient_rate=body.inpatient_rate, meal_rate=body.meal_rate,
        special_case_rate=body.special_case_rate,
        effective_from=body.effective_from, note=body.note, created_by=user.user_id)
    session.add(row)
    await _audit.record_audit(session, actor=user.user_id, action="CREATE",
                              entity="regulatory.institution_grade_settings",
                              after={"grade": body.grade,
                                     "outpatient_rate": body.outpatient_rate,
                                     "effective_from": str(body.effective_from)})
    return {"grade": body.grade, "effective_from": body.effective_from}


class DrugConfirmBody(BaseModel):
    his_code: str
    insurance_code: str


@router.post("/admin/regulatory/drug-map/build")
async def drug_map_build(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """미등재 약품 → 매칭 후보 SUGGESTED 일괄 빌드 (약품 코드 매핑 0053)."""
    from erp.regulatory.drug_map_service import DrugMapService
    _require(user, "regulatory.master", "write")
    return await DrugMapService().build_suggestions(session, actor=user.user_id)


@router.get("/admin/regulatory/drug-map")
async def drug_map_list(status: str | None = None, page: int = 1, size: int = 50,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """약품 코드 매핑 목록(페이지네이션) — 후보·신뢰도·확정상태(약제팀 검토)."""
    from erp.regulatory.drug_map_service import DrugMapService
    _require(user, "regulatory.master", "read")
    size = min(max(size, 1), 200)
    out = await DrugMapService().list_maps(session, status=status, page=page, size=size)
    return {"count": out["total"], "items": out["items"],
            "page": out["page"], "size": out["size"]}


@router.post("/admin/regulatory/drug-map/confirm")
async def drug_map_confirm(body: DrugConfirmBody,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """보험코드 확정 — 이후 동일 his_code 라인 자동 변환(price_source=MAP)."""
    from erp.regulatory.drug_map_service import DrugMapService
    _require(user, "regulatory.master", "write")
    return await DrugMapService().confirm(session, actor=user.user_id,
                                          his_code=body.his_code,
                                          insurance_code=body.insurance_code)


@router.post("/admin/regulatory/drug-map/{his_code}/reject")
async def drug_map_reject(his_code: str,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    from erp.regulatory.drug_map_service import DrugMapService
    _require(user, "regulatory.master", "write")
    return await DrugMapService().reject(session, actor=user.user_id, his_code=his_code)


@router.post("/admin/integrations/billing/sync-noncovered")
async def billing_sync_noncovered(user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """HIS 비급여 가격표 pull → fee_master 기관가 적재 (멱등·가격 갱신)."""
    from erp.integrations.his_billing import sync_noncovered
    _require(user, "admin.staging", "write")   # GL/마스터 변경 → write 게이트(read 금지·전체검토)
    out = await sync_noncovered(session)
    await _audit.record_access(session, actor=user.user_id, action="UPDATE",
                               entity="regulatory.fee_master(NONCOVERED)",
                               route="/admin/integrations/billing/sync-noncovered")
    return out


@router.post("/admin/integrations/billing/sync-materials")
async def billing_sync_materials(user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """HIS InventoryItem pull → scm.items 무매핑 등록."""
    from erp.integrations.his_billing import sync_materials
    _require(user, "admin.staging", "write")   # GL/마스터 변경 → write 게이트(read 금지·전체검토)
    out = await sync_materials(session)
    await _audit.record_access(session, actor=user.user_id, action="UPDATE",
                               entity="scm.items(HIS)",
                               route="/admin/integrations/billing/sync-materials")
    return out


@router.post("/admin/integrations/inbound/consume")
async def inbound_consume(reprocess: bool = False,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """인박스 수동 소비/재처리 (관리자) — reprocess=true면 FAILED→NEW 후 소비."""
    from sqlalchemy import update

    from erp.integrations.inbound_consumer import InboundConsumer
    from erp.integrations.models import HisInboundEvent

    _require(user, "admin.staging", "write")   # 소비=전표 생성(머니 변경) → write 게이트(read 금지)
    if reprocess:
        await session.execute(update(HisInboundEvent)
                              .where(HisInboundEvent.status == "FAILED")
                              .values(status="NEW", error=None))
    out = await InboundConsumer().consume(session)
    await _audit.record_access(session, actor=user.user_id, action="UPDATE",
                               entity="integration.his_inbound_events",
                               route="/admin/integrations/inbound/consume")
    return out


# ── ERP 내부 권한 배분 (HIS는 접근만, 내부 역할은 ERP 관리자가 별도) ──────
_ROLES = ("DIRECTOR", "ADMIN", "MANAGER", "STAFF",
          "IT_MANAGER", "FINANCE_MANAGER", "LOGISTICS_MANAGER")


class UserRoleBody(BaseModel):
    role: str


class UserActiveBody(BaseModel):
    active: bool


@router.get("/admin/users")
async def list_users(role: str | None = None, active: bool | None = None,
                     user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """ERP 사용자 목록 — 역할·활성 필터. DIRECTOR/ADMIN."""
    from erp.common.models import User
    _require(user, "admin.users", "read")
    q = select(User).order_by(User.id)
    if role:
        q = q.where(User.role == role)
    if active is not None:
        q = q.where(User.is_active == active)
    rows = (await session.execute(q.limit(2000))).scalars().all()  # 방어적 상한
    # 대소문자만 다른 중복 이메일 — SSO가 fail-closed로 거부하므로(H2) 관리자가 보고 정리할 수 있게 표시
    from sqlalchemy import func as _f
    dup_keys = set((await session.execute(
        select(_f.lower(User.email)).group_by(_f.lower(User.email))
        .having(_f.count() > 1))).scalars().all())
    # 재입사 후보 — 연결 직원이 퇴사했고 같은 이메일의 재직 직원이 1명. 동일인 판단은 사람(재연결 동작)
    cand = await _relink_candidates_bulk(session, rows)
    return {"items": [{"id": u.id, "email": u.email, "role": u.role,
                       "department_id": u.department_id, "employee_id": u.employee_id,
                       "is_active": u.is_active, "license_flag_active": u.license_flag_active,
                       "email_duplicate": u.email.lower() in dup_keys,
                       "relink_candidate": cand.get(u.id)}
                      for u in rows]}


async def _relink_candidates_bulk(session, users) -> dict[int, int]:
    """사용자 목록용 재입사 후보 일괄 계산 — 쿼리 2회(7차 검토: 사용자마다 조회하던 N+1 제거).
    판정 규칙은 identity.relink_candidate와 같다(연결 직원 퇴사 + 같은 이메일 재직 직원 정확히 1명)."""
    from sqlalchemy import text as _text

    from erp.common.identity import norm_email
    from erp.common.kst import kst_today
    linked = [u for u in users if u.employee_id is not None]
    if not linked:
        return {}
    today = kst_today().isoformat()
    emp_rows = (await session.execute(_text(
        "SELECT id, resigned_on, lower(trim(email)) FROM hr.employees"))).all()
    resigned = {r[0]: r[1] for r in emp_rows}
    active_by_email: dict[str, list[int]] = {}
    for eid, ro, em in emp_rows:
        if em and (ro is None or str(ro) >= today):
            active_by_email.setdefault(em, []).append(eid)
    out = {}
    for u in linked:
        ro = resigned.get(u.employee_id)
        if ro is None or str(ro) >= today:
            continue
        act = active_by_email.get(norm_email(u.email), [])
        if len(act) == 1 and act[0] != u.employee_id:
            out[u.id] = act[0]
    return out


class RelinkBody(BaseModel):
    employee_id: int


@router.post("/admin/users/{user_id}/relink")
async def relink_user(user_id: int, body: RelinkBody,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """재입사 재연결 — 퇴사 직원 기록에 연결된 계정을 같은 이메일의 재직 기록으로 옮기고 활성화.

    재입사(같은 사람)와 공용 이메일(다른 사람)은 이메일로 구분할 수 없어 자동으로 하지 않는다(6차 검토).
    관리자가 동일인임을 확인하고 실행한다. 대상은 relink_candidate가 가리키는 직원만 허용. ADMIN·감사.
    """
    from erp.common.identity import relink_candidate
    from erp.common.models import User
    _require(user, "admin.users", "write")
    target = await session.get(User, user_id)
    if target is None:
        raise ValidationError("존재하지 않는 사용자입니다")
    cand = await relink_candidate(session, employee_id=target.employee_id, email=target.email)
    if cand is None or cand != body.employee_id:
        raise ValidationError("재연결 대상이 아닙니다 — 연결 직원이 퇴사했고 같은 이메일의 재직 직원이 "
                              "한 명일 때만 그 직원으로 옮길 수 있습니다")
    before = target.employee_id
    target.employee_id = body.employee_id
    target.is_active = True
    await _audit.record_audit(session, actor=user.user_id, action="USER_RELINK_REHIRE",
                              entity="common.users", target_id=str(user_id),
                              before={"employee_id": before}, after={"employee_id": body.employee_id,
                                                                     "is_active": True})
    return {"id": user_id, "employee_id": target.employee_id, "is_active": True}


@router.post("/admin/users/{user_id}/retire-duplicate")
async def retire_duplicate_user(user_id: int,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """대소문자만 다른 중복 계정 정리 — 이 행을 비활성화하고 이메일을 보관용으로 바꿔 SSO 매칭에서 뺀다.

    SSO는 중복 행 중 하나라도 비활성이면 거부한다(fail-closed·H2). 우회로 생긴 행을 비활성화만 하면 진짜
    사용자까지 막히므로(3차 검토 I2) 정리는 이 동작으로 한다. 같은 이메일(대소문자 무관)의 다른 행이 있어야
    한다 — 단독 계정 삭제 수단이 아니다. ADMIN·감사.
    """
    from sqlalchemy import func as _f

    from erp.common.auth_service import AuthService
    from erp.common.models import User
    from erp.common.redis_client import get_redis
    _require(user, "admin.users", "write")
    if user_id == user.user_id:
        raise ValidationError("본인 계정은 정리할 수 없습니다")
    target = await session.get(User, user_id)
    if target is None:
        raise ValidationError("존재하지 않는 사용자입니다")
    others = await session.scalar(select(_f.count()).select_from(User).where(
        _f.lower(User.email) == target.email.lower(), User.id != user_id))
    if not others:
        raise ValidationError("대소문자 중복 계정이 아닙니다 — 이 동작은 중복 정리 전용입니다")
    before = target.email
    # 정리 뒤 남는 활성 행이 퇴사자(퇴사일 경과) 직원에 연결돼 있으면 거부(4차 검토 D1) — 비활성(차단된)
    # 행을 치워 fail-closed를 풀면 남은 활성 행으로 퇴사자가 SSO에 들어온다.

    from erp.common.identity import resigned_block_reason
    remaining = (await session.execute(select(User).where(
        _f.lower(User.email) == before.lower(), User.id != user_id,
        User.is_active.is_(True)))).scalars().all()
    for r in remaining:
        # SSO와 같은 판정 함수(common.identity) — 정리 뒤 남는 활성 행으로 퇴사자가 들어오면 거부(D1),
        # 관리자가 재활성한 처리된 퇴사는 SSO처럼 허용(5차 검토: 정리가 SSO보다 엄격해 영구 차단되던 문제)
        if await resigned_block_reason(session, employee_id=r.employee_id, email=before,
                                       existing_user=True):
            raise ValidationError(f"남는 활성 계정 #{r.id}이 퇴사 처리된 직원입니다 — 그 계정을 먼저 "
                                  "비활성화하거나 정리하세요(퇴사자 SSO 차단 유지)")
    target.email = f"retired-dup-{user_id}.{before}"[:255]
    await AuthService(redis=get_redis()).deactivate(session, user=target)
    await _audit.record_audit(session, actor=user.user_id, action="USER_DUPLICATE_RETIRE",
                              entity="common.users", target_id=str(user_id),
                              before={"email": before}, after={"email": target.email})
    return {"id": user_id, "email": target.email, "is_active": False}


@router.patch("/admin/users/{user_id}/role")
async def change_user_role(user_id: int, body: UserRoleBody,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """ERP 내부 역할 변경 — ADMIN만. 본인 변경 금지(자기잠금 방지)·강제 재인증·감사."""
    from erp.common.auth_service import AuthService
    from erp.common.models import User
    _require(user, "admin.users", "write")
    if body.role not in _ROLES:
        raise ValidationError(f"허용되지 않은 역할: {body.role} (가능: {', '.join(_ROLES)})")
    if user_id == user.user_id:
        raise ValidationError("본인 역할은 변경할 수 없습니다 (자기잠금 방지)")
    target = await session.get(User, user_id)
    if target is None:
        raise ValidationError("존재하지 않는 사용자입니다")
    before = target.role
    if before == body.role:
        return {"id": user_id, "role": before, "unchanged": True}
    # 마지막 ADMIN 강등 차단(잠금 방지)
    if before == "ADMIN" and body.role != "ADMIN":
        from sqlalchemy import func
        admins = await session.scalar(select(func.count()).select_from(User)
                                      .where(User.role == "ADMIN", User.is_active.is_(True)))
        if (admins or 0) <= 1:
            raise ValidationError("마지막 ADMIN은 강등할 수 없습니다")
    target.role = body.role
    await AuthService().bump_token_version(session, user_id=user_id)   # 즉시 재인증
    await _audit.record_audit(session, actor=user.user_id, action="USER_ROLE_CHANGE",
                              entity="common.users", target_id=str(user_id),
                              before={"role": before}, after={"role": body.role})
    return {"id": user_id, "role": target.role, "before": before}


@router.patch("/admin/users/{user_id}/active")
async def set_user_active(user_id: int, body: UserActiveBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """ERP 사용자 활성/비활성 — ADMIN만. 본인 비활성 금지·비활성 시 강제 로그아웃·감사."""
    from erp.common.auth_service import AuthService
    from erp.common.models import User
    _require(user, "admin.users", "write")
    if user_id == user.user_id:
        raise ValidationError("본인 계정은 비활성할 수 없습니다")
    target = await session.get(User, user_id)
    if target is None:
        raise ValidationError("존재하지 않는 사용자입니다")
    if body.active:
        target.is_active = True
    else:
        # refresh 전량 폐기+tv 증가+tv 캐시 삭제 — tv만 올리면 refresh·캐시로 세션이 남았다(G1)
        from erp.common.redis_client import get_redis
        await AuthService(redis=get_redis()).deactivate(session, user=target)
    await _audit.record_audit(session, actor=user.user_id, action="USER_ACTIVE_CHANGE",
                              entity="common.users", target_id=str(user_id),
                              after={"is_active": body.active})
    return {"id": user_id, "is_active": target.is_active}


# ── 역할별 메뉴 노출 설정 (DB 오버라이드) ──────────────────
class MenuOverrideBody(BaseModel):
    overrides: list[dict]   # [{menu_key, role, visible}]


class MenuOrgDisableBody(BaseModel):
    """기관 단위 비활성 메뉴(전량 치환) — 그룹웨어 등 외부 시스템과 중복되는 화면 숨김."""
    menu_keys: list[str]


@router.get("/admin/menu-permissions")
async def get_menu_permissions(user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """메뉴×역할 노출 매트릭스 (기본+오버라이드). DIRECTOR/ADMIN."""
    from erp.common.menu_service import MenuService
    _require(user, "admin.users", "read")
    return await MenuService().matrix(session)


@router.put("/admin/menu-permissions")
async def put_menu_permissions(body: MenuOverrideBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """메뉴 노출 오버라이드 일괄 저장 — ADMIN. 감사."""
    from erp.common.menu_service import MenuService
    _require(user, "admin.users", "write")
    n = await MenuService().set_overrides(session, items=body.overrides, actor=user.user_id)
    await _audit.record_audit(session, actor=user.user_id, action="MENU_PERM_UPDATE",
                              entity="common.menu_permissions", target_id=str(n),
                              after={"count": n})
    return {"updated": n}


@router.put("/admin/menu-org-disabled")
async def put_menu_org_disabled(body: MenuOrgDisableBody,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """기관 단위 메뉴 비활성 저장 — ADMIN. 감사.

    그룹웨어 전자결재가 별도로 있는 기관에서 ERP 셀프서비스 화면(휴가 신청·경비청구·근태 등)이
    **중복 입구**가 되는 것을 한 번에 끄기 위한 스위치. 역할 무관 전역이라 역할별 오버라이드를
    7개 다 끄다 하나를 놓치는 사고가 없다. **메뉴 노출만** 바꾸고 권한·EP·데이터는 불변.
    """
    from erp.common.menu_service import MenuService
    _require(user, "admin.users", "write")
    keys = await MenuService().set_org_disabled(
        session, menu_keys=body.menu_keys, actor=user.user_id)
    await _audit.record_audit(session, actor=user.user_id, action="MENU_ORG_DISABLE",
                              entity="common.org_settings", target_id="menu",
                              after={"disabled": keys})
    return {"disabled_menu_keys": keys}


@router.get("/admin/consistency")
async def consistency_checks(user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """운영 정합성 점검 — 도메인 경계를 가로지르는 불변식 상시 검사.

    지금까지 정합 문제(외화 GL 통화 혼입·미러만 있고 GL 없는 채권·병합이 남긴 고아 전표·
    환자 마스터 불일치)는 사람이 의심할 때만 손으로 SQL을 돌려 찾았고, 전부 **조용히 틀린 채로
    있다가 우연히 드러났다.** 실 운영에서는 우연에 맡길 수 없어 같은 검사를 기능으로 고정한다.
    각 검사는 정상 상태에서 반드시 OK가 되는 불변식이다(영구 발화 금지 — 활동 기반 검사는
    조건부로 만들어 전제가 없으면 SKIP).
    """
    from erp.app.consistency_service import run_all
    _require(user, "admin.audit", "read")
    out = await run_all(session)
    await _audit.record_access(session, actor=user.user_id, action="VIEW",
                               entity="app.consistency", route="/admin/consistency")
    return out


# ── 기관 설정 (공식 문서 발급기관 표기 SoT) ─────────────────
class OrgSettingsBody(BaseModel):
    name: str                            # 기관 정식명칭(필수)
    representative: str | None = None    # 대표자(병원장)
    biz_reg_no: str | None = None        # 사업자등록번호
    care_org_no: str | None = None       # 요양기관기호(문서 표기용 — 연동용 env hospital_code와 별개)
    address: str | None = None
    tel: str | None = None
    domain: str | None = None            # 기관 도메인 주소
    fiscal_year_start_month: int | None = None   # 회계연도 시작월(1~12·기본 1=역년)
    extra: list[dict] | None = None      # 기타 커스텀 [{key,value}]


def _org_out(row, source: str) -> dict:
    return {"configured": row is not None, "source": source,
            "name": getattr(row, "name", None),
            "representative": getattr(row, "representative", None),
            "biz_reg_no": getattr(row, "biz_reg_no", None),
            "care_org_no": getattr(row, "care_org_no", None),
            "address": getattr(row, "address", None), "tel": getattr(row, "tel", None),
            "domain": getattr(row, "domain", None), "extra": getattr(row, "extra", None),
            "fiscal_year_start_month": getattr(row, "fiscal_year_start_month", None) or 1,
            "updated_at": getattr(row, "updated_at", None)}


@router.get("/admin/org-settings")
async def get_org_settings(user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """기관 설정 조회 — DIRECTOR/ADMIN. 미설정 시 env ORG_NAME 폴백 표시(source로 구분)."""
    from erp.common.org_service import OrgSettingsService
    from erp.config import get_settings
    _require(user, "admin.org", "read")
    row = await OrgSettingsService().get(session)
    if row is not None:
        return _org_out(row, "db")
    env = (get_settings().org_name or "").strip()
    out = _org_out(None, "env" if env else "unset")
    out["name"] = env or None            # env 폴백 표시(문서엔 "본 의료기관" 폴백)
    return out


@router.put("/admin/org-settings")
async def put_org_settings(body: OrgSettingsBody,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """기관 설정 저장(단일행 upsert) — ADMIN. 감사기록."""
    from erp.common.org_service import OrgSettingsService
    from erp.errors import ValidationError
    from erp.finance.models import Voucher
    _require(user, "admin.org", "write")
    svc = OrgSettingsService()
    # 결산 전표 존재 시 회계연도 시작월 변경 차단 — 멱등키 closing:{연도}가 옛 회계연도 창을 가리켜
    # 시작월 변경 시 특정 기간 손익이 이익잉여금 이전에서 누락/이중(적대검토 MED/HIGH). finance 조회는
    # 앱 계층에서(common은 순수 커널이라 finance 의존 금지).
    if await svc.fiscal_month_would_change(session, body.fiscal_year_start_month):
        closed = await session.scalar(select(Voucher.id).where(
            Voucher.source_module == "closing").limit(1))
        if closed is not None:
            raise ValidationError(
                "이미 연말결산 전표가 존재해 회계연도 시작월을 변경할 수 없습니다 "
                "(기존 결산의 기간 정합이 깨짐 — 결산 취소 후 변경하십시오)")
    row = await svc.upsert(session, actor=user.user_id, **body.model_dump())
    await _audit.record_audit(session, actor=user.user_id, action="ORG_SETTINGS_UPDATE",
                              entity="common.org_settings", target_id="1",
                              after={"name": row.name, "biz_reg_no": row.biz_reg_no})
    return _org_out(row, "db")


class PayrollSettingsBody(BaseModel):
    incentive_withholding_method: str = "NONE"    # NONE(정산) | FLAT(정률) | COMBINED(상여합산)
    incentive_withholding_rate: float = 0.0       # FLAT 정률(0~1, 예 0.033)
    combined_months: int = 1                      # COMBINED 상여 지급대상기간(1~12)
    employer_burden_posting: bool = False         # 사업주 4대보험 부담금 GL 전기
    employer_ei_rate: float = 0.0115              # 고용보험 사업주 요율
    employer_wc_rate: float = 0.007               # 산재 요율(업종별)
    night_pay_mode: str = "FLAT"                  # 야간수당: FLAT(정액)|STATUTORY(통상시급×0.5)|MAX
    incentive_grade_link: bool = False            # 인사평가 등급 → 성과급 배율(P1-1·옵트인)


def _payroll_settings_out(row, eff: dict) -> dict:
    return {"configured": row is not None, **eff,
            "updated_at": getattr(row, "updated_at", None)}


@router.get("/admin/payroll-settings")
async def get_payroll_settings(user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """급여 정책 설정 조회 — 성과급 원천징수 방식 등. 미설정 시 기본값(NONE) 표시."""
    from erp.hr.payroll_settings import PayrollSettingsService
    # 2026-10-09 전체 흐름 검토 G — hr.payroll read는 STAFF에도 SELF로 열려 있는데 기관 급여 정책은
    # 행 필터가 불가한 전사 설정이라 ALL만 허용(STAFF가 원천징수 방식·사업주 요율을 조회하던 결함).
    from erp.common.policy import ScopeKind
    from erp.errors import ForbiddenError
    if _require(user, "hr.payroll", "read").kind != ScopeKind.ALL:
        raise ForbiddenError("급여 정책 설정은 관리자만 조회할 수 있습니다")
    svc = PayrollSettingsService()
    return _payroll_settings_out(await svc.get(session), await svc.effective(session))


@router.put("/admin/payroll-settings")
async def put_payroll_settings(body: PayrollSettingsBody,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """급여 정책 설정 저장(단일행 upsert) — hr.payroll write. 감사기록."""
    from erp.hr.payroll_settings import PayrollSettingsService
    _require(user, "hr.payroll", "write")
    svc = PayrollSettingsService()
    row = await svc.upsert(session, actor=user.user_id, **body.model_dump())
    await _audit.record_audit(session, actor=user.user_id, action="PAYROLL_SETTINGS_UPDATE",
                              entity="hr.payroll_settings", target_id="1",
                              after={"method": row.incentive_withholding_method,
                                     "rate": float(row.incentive_withholding_rate),
                                     "night_pay_mode": row.night_pay_mode})
    return _payroll_settings_out(row, await svc.effective(session))


@router.get("/retention/candidates")
async def retention_candidates(as_of: date | None = None,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """보존연한 경과 파기 대상 식별 (design §6.5) — 카테고리별 건수(읽기 전용·ADMIN).

    실제 파기는 관리자 승인 후 별도 수행(불가역이라 자동삭제 없음). 보존연한은 법무 검토 권장."""
    _require(user, "admin.retention", "read")
    from erp.app.retention_service import RetentionService
    return await RetentionService().identify_candidates(session, as_of=as_of or date.today())


# ── 결정 대기함 (0226) — 사람 결정 항목 관리(등록·결정·보류·재오픈) ─────────
class DecisionCreateBody(BaseModel):
    key: str
    title: str
    question: str
    options: list[dict]                     # [{key,label,description,recommended?}]
    category: str = "GENERAL"
    context_ref: str | None = None


class DecisionDecideBody(BaseModel):
    option: str
    note: str | None = None


class DecisionNoteBody(BaseModel):
    note: str | None = None


def _decision_dict(d) -> dict:
    return {"id": d.id, "key": d.key, "category": d.category, "title": d.title,
            "question": d.question, "options": d.options, "context_ref": d.context_ref,
            "status": d.status, "decided_option": d.decided_option,
            "decided_note": d.decided_note, "decided_by": d.decided_by,
            "decided_at": d.decided_at, "created_at": d.created_at,
            "execute_screen": _execute_screen(d.key)}


def _execute_screen(key: str) -> str | None:
    """선택=실행인 결정의 실행 화면 — 결정 대기함은 결정 버튼 대신 이 화면으로 안내한다."""
    from erp.hr import rate_settlement as _rs
    return _rs.DECISION_SCREEN if key in _rs.DECISION_BY_SOURCE.values() else None


def _category(raw: str) -> str:
    """결정 분류 — 결정·조치함 분류 단일 소스(action_center.CATEGORIES)의 키만 받는다(제각각 분류 방지)."""
    from erp.app.action_center import CATEGORIES, CATEGORY_KEYS
    c = (raw or "").strip().upper()
    if c not in CATEGORY_KEYS:
        raise ValidationError("알 수 없는 분류입니다", {"category": raw,
                                                    "allowed": [k for k, _ in CATEGORIES]})
    return c


@router.get("/admin/schedule")
async def schedule_month(yyyymm: YearMonth, user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """일정 캘린더 — 그 달 정기업무·법정 기한·공휴일·이의신청 기한(권한 있는 소스만). 저장 없음·각 항목은 처리 화면 링크."""
    from erp.app import schedule_service
    from erp.common.recurring import now_naive
    _require(user, "admin.recurring", "read")
    return await schedule_service.month(session, user=user, yyyymm=yyyymm, now=now_naive())


# ── 배치 작업(worker-batch) 관리 — 목록·일정·이력·지금 실행·일시정지 (v1.302.0) ─────────────
class BatchCommandBody(BaseModel):
    reason: str


@router.get("/admin/batch-jobs")
async def batch_jobs_overview(user: CurrentUser = Depends(get_current_user)):
    """잡 목록(카탈로그)·주기·다음 실행·마지막 상태·일시정지·실행기 하트비트. Redis 불가면 redis_ok=False(목록만)."""
    from erp.common import batch_jobs
    from erp.common.redis_client import get_redis
    _require(user, "admin.recurring", "read")
    try:
        return {"redis_ok": True, **await batch_jobs.overview(get_redis())}
    except Exception as exc:  # noqa: BLE001 — Redis 장애여도 화면은 잡 목록과 원인을 보인다
        return {"redis_ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}", "heartbeats": {},
                "schedule_published": False, "recent_commands": [],
                "jobs": [{"id": k, "label": v[0], "description": v[1], "can_pause": v[2], "can_run": v[3]}
                         for k, v in batch_jobs.CATALOG.items()]}


@router.get("/admin/batch-jobs/{job_id}/history")
async def batch_job_history(job_id: str, user: CurrentUser = Depends(get_current_user)):
    from erp.common import batch_jobs
    from erp.common.redis_client import get_redis
    _require(user, "admin.recurring", "read")
    if job_id not in batch_jobs.CATALOG:
        raise ValidationError("등록되지 않은 배치 작업입니다", {"job": job_id})
    try:
        return {"job": job_id, "redis_ok": True, "items": await batch_jobs.history(get_redis(), job_id)}
    except Exception as exc:  # noqa: BLE001 — Redis 장애여도 500 대신 원인
        return {"job": job_id, "redis_ok": False, "items": [], "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


@router.post("/admin/batch-jobs/{job_id}/{op}")
async def batch_job_command(job_id: str, op: str, body: BatchCommandBody,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """지금 실행·일시정지·재개 — 사유 필수·감사. 스케줄러 리더가 30초 안에 반영(결과는 목록의 최근 조작)."""
    from erp.common import batch_jobs
    from erp.common.redis_client import get_redis
    _require(user, "admin.recurring", "write")
    if op not in ("run", "pause", "resume"):
        raise ValidationError("알 수 없는 조작입니다", {"op": op})
    # 감사 먼저(플러시) — 감사 기록이 실패하면 명령을 내지 않는다(감사 없는 조작 방지)
    await _audit.record_audit(session, actor=user.user_id, action=f"BATCH_{op.upper()}", entity="infra.batch_jobs",
                              target_id=job_id, after={"reason": (body.reason or "").strip()[:200]})
    await session.flush()
    return await batch_jobs.enqueue(get_redis(), op=op, job_id=job_id, reason=body.reason, actor=user.user_id)


@router.get("/admin/decisions/actions")
async def decision_screen_actions(user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """결정·조치함의 '화면 조치' — 결정이 아니라 다른 화면에서 처리할 사람 몫의 일(현재 데이터로 계산·처리하면 사라짐)."""
    from erp.app.action_center import screen_actions
    from erp.common.kst import kst_today
    _require(user, "admin.decisions", "read")
    return await screen_actions(session, today=kst_today())


@router.get("/admin/decisions")
async def list_decisions(status: str | None = None,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """결정 대기함 목록 — PENDING 우선 정렬."""
    from erp.common.models import PendingDecision
    _require(user, "admin.decisions", "read")
    q = select(PendingDecision)
    if status:
        q = q.where(PendingDecision.status == status.upper())
    rows = (await session.scalars(q.order_by(
        PendingDecision.status.desc(), PendingDecision.id))).all()   # PENDING > DEFERRED > DECIDED
    return {"items": [_decision_dict(d) for d in rows]}


@router.post("/admin/decisions", status_code=201)
async def create_decision(body: DecisionCreateBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """결정 항목 등록 — 감사/기획의 사람 결정 승격분을 수동 추가."""
    from erp.common.models import PendingDecision
    _require(user, "admin.decisions", "write")
    if not body.options or not all(o.get("key") and o.get("label") for o in body.options):
        raise ValidationError("options는 [{key,label,…}] 1개 이상이어야 합니다")
    if await session.scalar(select(PendingDecision).where(PendingDecision.key == body.key)):
        raise ValidationError("이미 존재하는 key입니다", {"key": body.key})
    d = PendingDecision(key=body.key[:64], category=_category(body.category),
                        title=body.title[:200], question=body.question, options=body.options,
                        context_ref=body.context_ref, created_by=user.user_id)
    session.add(d)
    await session.flush()
    return {"id": d.id, "key": d.key}


class DecisionBulkSkipBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=500)


@router.post("/admin/decisions/bulk-skip")
async def bulk_skip_ai_drafts(body: DecisionBulkSkipBody,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """정기업무 AI 초안(rectask:*) 여러 건을 '반려(생략)'로 — 전부 아니면 전무·건별 감사.

    2026-10-10 prod: 주간 미수금 점검 초안이 W31~W41 11건 쌓여 진짜 결정 25건을 가렸다. 주간 점검은 시점 스냅샷이라
    다음 주 초안이 생기면 이전 것은 검토 의미가 없다 — 관리자가 골라 한 번에 생략한다. **일괄 승인은 두지 않는다**
    (승인=검토 완료라 건별로만). 반려된 run은 다음 정기업무 실행 때 sync_ai_decisions가 SKIPPED로 확정한다.
    """
    from datetime import timezone as _tz

    from erp.common.models import PendingDecision
    _require(user, "admin.decisions", "write")
    note = (body.note or "").strip()
    if not note:
        raise ValidationError("생략 사유를 입력하세요")
    ids = sorted(set(body.ids))
    rows = (await session.scalars(select(PendingDecision).where(PendingDecision.id.in_(ids))
                                  .order_by(PendingDecision.id).with_for_update())).all()
    missing = sorted(set(ids) - {d.id for d in rows})
    if missing:
        raise ValidationError(f"존재하지 않는 결정 항목: {', '.join(map(str, missing))}")
    bad = [d.key for d in rows if not (d.key.startswith("rectask:") and d.status == "PENDING"
                                       and "reject" in {o.get("key") for o in (d.options or [])})]
    if bad:
        raise ValidationError("결정 대기 중인 정기업무 AI 초안만 일괄 생략할 수 있습니다 — 아무것도 바꾸지 않았습니다: "
                              + ", ".join(bad[:10]))
    now = datetime.now(_tz.utc)
    for d in rows:
        d.status, d.decided_option = "DECIDED", "reject"
        d.decided_note = f"[일괄 생략] {note}"[:1000]
        d.decided_by, d.decided_at = user.user_id, now
        await _audit.record_audit(session, actor=user.user_id, action="DECIDE",
                                  entity="common.pending_decisions", target_id=d.key,
                                  after={"option": "reject", "note": d.decided_note, "bulk": True})
    out = {"skipped": [d.key for d in rows]}
    await session.commit()
    return out


class DecisionUpdateBody(BaseModel):
    question: str | None = None
    options: list[dict] | None = None
    context_ref: str | None = None
    title: str | None = None


async def _get_decision(session, decision_id: int):
    from erp.common.models import PendingDecision
    d = await session.get(PendingDecision, decision_id, with_for_update=True)
    if d is None:
        raise ValidationError("존재하지 않는 결정 항목입니다", {"id": decision_id})
    return d


async def _executed_decision_guard(session, d, action: str) -> None:
    """실행 화면이 있는 결정(요율 차액 정산 등)은 선택=실행이라 여기서 결정·재오픈하지 않는다.

    결정만 기록되고 실행이 안 되면 '결정했는데 아무 일도 없는' 상태가 된다(v1.295.0). 재오픈은 활성 실행이
    없을 때만(실행 취소가 재오픈을 함께 한다).
    """
    from erp.hr import rate_settlement as _rs
    src = next((m for m, k in _rs.DECISION_BY_SOURCE.items() if k == d.key), None)
    if src is None:
        return
    if action == "decide":
        raise ValidationError(f"이 결정은 실행 화면에서 방식을 고르면 실행과 함께 기록됩니다 — {_rs.DECISION_SCREEN} "
                              "'요율 차액 정산'", {"screen": _rs.DECISION_SCREEN})
    if await _rs._active(session, src):
        raise ValidationError(f"실행된 정산이 있습니다 — {_rs.DECISION_SCREEN} '요율 차액 정산'에서 취소하면 결정도 "
                              "재오픈됩니다", {"screen": _rs.DECISION_SCREEN})


@router.post("/admin/decisions/{decision_id}/decide")
async def decide_decision(decision_id: int, body: DecisionDecideBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """결정 기록 — 선택지 검증·감사로그. 결정 후 재오픈 전까지 변경 불가."""
    _require(user, "admin.decisions", "write")
    d = await _get_decision(session, decision_id)
    if d.status == "DECIDED":
        raise ValidationError("이미 결정된 항목입니다 — 변경은 재오픈 후", {"status": d.status})
    await _executed_decision_guard(session, d, "decide")
    if body.option not in {o.get("key") for o in (d.options or [])}:
        raise ValidationError("유효하지 않은 선택지입니다", {"option": body.option})
    from datetime import timezone as _tz
    d.status, d.decided_option = "DECIDED", body.option
    d.decided_note = (body.note or "")[:1000] or None
    d.decided_by, d.decided_at = user.user_id, datetime.now(_tz.utc)
    await _audit.record_audit(session, actor=user.user_id, action="DECIDE",
                              entity="common.pending_decisions", target_id=d.key,
                              after={"option": body.option, "note": d.decided_note})
    return _decision_dict(d)


@router.patch("/admin/decisions/{decision_id}")
async def update_decision(decision_id: int, body: DecisionUpdateBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """미결 항목의 질문·선택지 갱신 — **상황이 변하면 결정문도 낡는다**.

    실제 사례(2026-07-30): `rectask-ai-assisted-scope`의 선택지 2개(주간 미수금·분기 부가세)가
    그 사이 **구현·배포·prod 검증까지 끝났는데** 결정문은 여전히 "무엇을 만들까"를 묻고 있었다.
    낡은 결정문은 결정을 미루게 만들고, 고치려면 DB를 손으로 만져야 했다(관리기능 부재).

    ⚠ **DECIDED는 갱신 금지** — 무엇을 보고 결정했는지가 바뀌면 결정 근거가 사라진다. 필요하면
    재오픈(사유 필수) 후 갱신한다. 갱신 전/후는 감사로그에 남는다.
    """
    _require(user, "admin.decisions", "write")
    d = await _get_decision(session, decision_id)
    if d.status == "DECIDED":
        raise ValidationError("결정된 항목은 갱신할 수 없습니다 — 재오픈 후 갱신하세요",
                              {"status": d.status})
    if body.options is not None:
        if not body.options or not all(o.get("key") and o.get("label") for o in body.options):
            raise ValidationError("options는 [{key,label,…}] 1개 이상이어야 합니다")
    before = {"question": d.question, "options": d.options, "title": d.title,
              "context_ref": d.context_ref}
    if body.question is not None:
        d.question = body.question
    if body.options is not None:
        d.options = body.options
    if body.context_ref is not None:
        d.context_ref = body.context_ref or None
    if body.title is not None:
        d.title = body.title[:200]
    await _audit.record_audit(session, actor=user.user_id, action="UPDATE",
                              entity="common.pending_decisions", target_id=d.key,
                              before=before,
                              after={"question": d.question, "options": d.options,
                                     "title": d.title, "context_ref": d.context_ref})
    return _decision_dict(d)


class DecisionCategoryBody(BaseModel):
    category: str


@router.post("/admin/decisions/{decision_id}/category")
async def recategorize_decision(decision_id: int, body: DecisionCategoryBody,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """분류 변경 — 정리 목적이라 결정된 항목도 허용(질문·선택지·결정은 그대로라 결정 근거 불변). 감사로그."""
    _require(user, "admin.decisions", "write")
    d = await _get_decision(session, decision_id)
    new = _category(body.category)
    if new != d.category:
        before = d.category
        d.category = new
        await _audit.record_audit(session, actor=user.user_id, action="UPDATE",
                                  entity="common.pending_decisions", target_id=d.key,
                                  before={"category": before}, after={"category": new})
    return _decision_dict(d)


@router.post("/admin/decisions/{decision_id}/defer")
async def defer_decision(decision_id: int, body: DecisionNoteBody | None = None,
                         user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _require(user, "admin.decisions", "write")
    d = await _get_decision(session, decision_id)
    if d.status == "DECIDED":
        raise ValidationError("결정된 항목은 보류할 수 없습니다", {"status": d.status})
    d.status = "DEFERRED"
    d.decided_note = ((body.note if body else None) or "")[:1000] or d.decided_note
    await _audit.record_audit(session, actor=user.user_id, action="DEFER",
                              entity="common.pending_decisions", target_id=d.key,
                              after={"note": d.decided_note})
    return _decision_dict(d)


@router.post("/admin/decisions/{decision_id}/reopen")
async def reopen_decision(decision_id: int, body: DecisionNoteBody,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """재오픈 — 결정/보류 항목을 PENDING으로(사유 필수·기존 결정값은 이력으로 audit_log에 잔존)."""
    _require(user, "admin.decisions", "write")
    if not (body.note or "").strip():
        raise ValidationError("재오픈 사유가 필요합니다")
    d = await _get_decision(session, decision_id)
    await _executed_decision_guard(session, d, "reopen")
    before = {"status": d.status, "option": d.decided_option}
    d.status, d.decided_option = "PENDING", None
    d.decided_note, d.decided_by, d.decided_at = None, None, None
    await _audit.record_audit(session, actor=user.user_id, action="REOPEN",
                              entity="common.pending_decisions", target_id=d.key,
                              before=before, after={"note": body.note[:500]})
    return _decision_dict(d)
