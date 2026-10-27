"""정기업무(주/월/분기) 관리 라우터 — 정의 조회/편집·due 현황·실행·완료·이력.

관리화면(/admin/recurring-tasks)과 AI CLI(erp.app.recurring_cli)가 공용. 게이트=admin.recurring
(read=원장·관리자 / write=관리자). requires_approval 업무는 즉시 실행 불가(서비스에서 차단).
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from erp.app.recurring_service import RecurringRunner
from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session

router = APIRouter(tags=["admin"], prefix="/admin/recurring-tasks")
_svc = RecurringRunner()


class TaskUpsertBody(BaseModel):
    name: str | None = None
    domain: str | None = None
    category: str | None = None
    cadence: str | None = None
    anchor: dict | None = None
    execution_mode: str | None = None
    handler_ref: str | None = None
    owner_role: str | None = None
    requires_approval: bool | None = None
    sla_days: int | None = None
    source_ref: str | None = None
    enabled: bool | None = None
    note: str | None = None


class CompleteBody(BaseModel):
    note: str | None = None


@router.get("")
async def list_tasks(user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    require_scope(user, "admin.recurring", "read")
    return {"tasks": await _svc.list_tasks(session)}


@router.get("/due")
async def due(user: CurrentUser = Depends(get_current_user),
              session: AsyncSession = Depends(get_session)):
    """현재 due/overdue 현황(기계친화) — AI CLI가 '무엇이 밀렸나'를 조회."""
    require_scope(user, "admin.recurring", "read")
    return {"due": await _svc.due_list(session)}


@router.get("/{code}/runs")
async def runs(code: str, limit: int = 50, user: CurrentUser = Depends(get_current_user),
               session: AsyncSession = Depends(get_session)):
    require_scope(user, "admin.recurring", "read")
    return {"runs": await _svc.list_runs(session, code=code, limit=limit)}


@router.put("/{code}")
async def upsert(code: str, body: TaskUpsertBody,
                 user: CurrentUser = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)):
    require_scope(user, "admin.recurring", "write")
    # 화면 편집도 감사에 남긴다 — 종전엔 기록이 없어 시드가 담당 역할을 되돌렸는지 사후에 알 수 없었다(2026-10-10)
    from sqlalchemy import select

    from erp.audit.service import AuditService
    from erp.common.models import RecurringTask
    changes = body.model_dump(exclude_none=True)
    t0 = await session.scalar(select(RecurringTask).where(RecurringTask.code == code))
    before = None if t0 is None else {k: getattr(t0, k, None) for k in changes}
    await AuditService().record_audit(session, actor=user.user_id, action="UPSERT",
                                      entity="common.recurring_tasks", target_id=code,
                                      before=before, after=changes)
    t = await _svc.upsert_task(session, code=code, actor=user.user_id, **changes)
    return {"code": t.code, "enabled": t.enabled}


@router.get("/registry/drift")
async def registry_drift(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """코드 정의 ↔ 레지스트리 차이(누락·값 다름·레지스트리 전용) — 관리화면 '정의 동기화'."""
    require_scope(user, "admin.recurring", "read")
    return await _svc.registry_drift(session)


class RegistrySyncBody(BaseModel):
    codes: list[str] = Field(min_length=1, max_length=100)


@router.post("/registry/sync")
async def registry_sync(body: RegistrySyncBody, user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """선택 업무를 코드 정의로 맞춤(누락=등록·다름=코드값) — 활성 여부는 보존·감사 기록."""
    require_scope(user, "admin.recurring", "write")
    return await _svc.registry_apply(session, codes=body.codes, actor=user.user_id)


class RegistryKeepBody(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


@router.post("/registry/{code}/keep")
async def registry_keep(code: str, body: RegistryKeepBody,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """코드 정의와 다른 현재 값을 의도된 것으로 확인(차이 목록에서 제외·사유 감사)."""
    require_scope(user, "admin.recurring", "write")
    return await _svc.registry_keep(session, code=code, actor=user.user_id, reason=body.reason)


@router.post("/{code}/run")
async def run(code: str, force: bool = False,
              user: CurrentUser = Depends(get_current_user),
              session: AsyncSession = Depends(get_session)):
    """AUTO/AI_ASSISTED 단건 실행(관리화면·AI CLI). requires_approval은 서비스에서 차단."""
    require_scope(user, "admin.recurring", "write")
    r = await _svc.run_one(session, code=code, actor=user.user_id,
                           triggered_by="ADMIN_UI", force=force)
    return {"code": code, "period_key": r.period_key, "status": r.status, "error": r.error}


@router.get("/infra-jobs")
async def infra_jobs(user: CurrentUser = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """APScheduler 잔류 인프라·연동잡 최종 실행 상태(R7 관측성) + 정기업무 이상 신호(R11).

    worker-batch의 이벤트 리스너가 Redis 해시에 기록한 잡별 (마지막 실행·상태·오류)를 조회.
    Redis 불가 시 잡 목록만 비우고 502가 아닌 정상 응답(신호는 DB 기반이라 독립 산출).
    """
    require_scope(user, "admin.recurring", "read")
    import json
    import time as _t
    from datetime import datetime
    from zoneinfo import ZoneInfo

    jobs, hb_age, redis_ok = [], None, True
    try:
        import redis.asyncio as aioredis

        from erp.config import get_settings
        # 키 리터럴 — workers.scheduler 역참조 회피(main.py 딥헬스와 동일 관례)
        r = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        raw = await r.hgetall("erp:infrajobs:status")
        hb = await r.get("erp:heartbeat:worker-batch")
        reg_raw = await r.get("erp:infrajobs:registry")   # 잡별 기대 발화 간격(스케줄러 기동 시 게시)
        await r.aclose()
        if hb is not None:
            hb_age = int(_t.time()) - int(hb)
        reg = json.loads(reg_raw) if reg_raw else {}
        for job_id, payload in raw.items():
            d = json.loads(payload)
            ts = datetime.fromtimestamp(d["ts"], ZoneInfo("Asia/Seoul"))
            age = int(_t.time()) - int(d["ts"])
            interval = reg.get(job_id)
            # 기대 간격의 3배를 넘도록 안 돌았으면 stale — 간격 정보 없이는 "6시간 전"을 초록으로만
            # 표시해 죽은 잡을 정상으로 오독하게 된다(적대검토 B-F7).
            stale = bool(interval) and age > int(interval) * 3
            jobs.append({"id": job_id, "status": d.get("status"),
                         "last_run_at": ts.replace(tzinfo=None).isoformat(sep=" "),
                         "age_s": age, "expected_interval_s": interval, "stale": stale,
                         "error": d.get("err") or None})
        jobs.sort(key=lambda j: j["id"])
    except Exception:  # noqa: BLE001 — Redis 불가(로컬 등)면 패널만 비움
        redis_ok = False
    return {"redis_ok": redis_ok, "worker_heartbeat_age_s": hb_age, "jobs": jobs,
            "signal": await _svc.health_signal(session)}


@router.get("/compliance-context")
async def compliance_context(horizon_days: int = 90,
                             user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    """관련 법정기한 링크아웃(읽기전용) — 정기업무와 함께 참고. compliance 원천 재사용."""
    require_scope(user, "admin.recurring", "read")
    return {"deadlines": await _svc.compliance_context(session, horizon_days=horizon_days)}


@router.post("/{code}/prepare")
async def prepare(code: str, user: CurrentUser = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    """AI_ASSISTED 초안 준비 → 결정 대기함 스테이징(WAITING_APPROVAL)."""
    require_scope(user, "admin.recurring", "write")
    r = await _svc.prepare_ai_assisted(session, code=code, actor=user.user_id)
    return {"code": code, "period_key": r.period_key, "status": r.status,
            "decision_key": r.decision_key, "error": r.error}


@router.post("/sync-decisions")
async def sync_decisions(user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """확정된 AI_ASSISTED 결정을 run에 반영(승인=완료·반려=생략)."""
    require_scope(user, "admin.recurring", "write")
    return await _svc.sync_ai_decisions(session)


@router.post("/{code}/complete")
async def complete(code: str, body: CompleteBody,
                   user: CurrentUser = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    """수기(MANUAL) 업무 완료 처리."""
    require_scope(user, "admin.recurring", "write")
    r = await _svc.complete_manual(session, code=code, actor=user.user_id, note=body.note)
    return {"code": code, "period_key": r.period_key, "status": r.status}


@router.post("/run-due")
async def run_due(dry_run: bool = False, user: CurrentUser = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    """자동실행 대상(AUTO) 일괄 실행 — 관리화면 버튼. dry_run=섀도우(물질화만). (CLI는 서비스 직호출)"""
    require_scope(user, "admin.recurring", "write")
    summary = await _svc.run_due(session, dry_run=dry_run, actor=user.user_id,
                                 triggered_by="ADMIN_UI")
    return summary
