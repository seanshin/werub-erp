"""worker-batch 스케줄러 — design §11.0. 모든 배치는 진료시간(08–18시) 외."""
import asyncio
import logging
import time
import uuid
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import erp.app.recurring_handlers  # noqa: F401 — 정기업무 AUTO 핸들러 등록(메타잡 실행용)
from erp.common.kst import KST, assert_process_tz_kst, kst_today
from erp.db import session_factory
from erp.logging import request_id_var, setup_logging

logger = logging.getLogger("erp.workers.scheduler")

HEARTBEAT_KEY = "erp:heartbeat:worker-batch"   # 딥 헬스가 나이 검사(죽은 워커 감지·P0-2)
HEARTBEAT_TTL_S = 180
INFRA_STATUS_KEY = "erp:infrajobs:status"      # R7 관측성 — 잡별 최종 실행 상태(관리화면 패널)
INFRA_STATUS_TTL_S = 7 * 86400                 # 잡 제거 후 유령 엔트리 자연 소멸
INFRA_JOBS_KEY = "erp:infrajobs:registry"      # 현재 등록 잡 → 기대 간격(화면 stale 판정)


def _job_request_id(name: str) -> None:
    request_id_var.set(f"job:{name}:{uuid.uuid4().hex[:8]}")  # design §5.2 전파 규약


async def _write_infra_status(job_id: str, status: str, err: str, scheduled_ts: int | None = None) -> None:
    try:
        import json

        import redis.asyncio as aioredis

        from erp.config import get_settings
        from erp.common import batch_jobs
        r = aioredis.from_url(get_settings().redis_url)
        if status != "skipped":                  # 건너뜀은 마지막 상태를 덮지 않는다(실행 중인 실행이 따로 끝난다)
            await r.hset(INFRA_STATUS_KEY, job_id,
                         json.dumps({"ts": int(time.time()), "status": status, "err": err}))
            await r.expire(INFRA_STATUS_KEY, INFRA_STATUS_TTL_S)
        if job_id != "batch_control" or status != "ok":   # 30초 조작 반영 잡의 정상 실행은 이력을 채우지 않는다
            await batch_jobs.record_run(r, job_id, status, err, scheduled_ts=scheduled_ts)
        await r.aclose()
    except Exception as exc:  # noqa: BLE001 — 관측 기록 실패는 비차단
        logger.warning("infra 잡 상태 기록 실패",
                       extra={"ctx": {"job": job_id, "err": str(exc)[:120]}})


def _expected_interval_s(trigger) -> int | None:
    """트리거의 기대 발화 간격(초) — 다음 두 발화 시각의 간격. 화면 stale 판정 기준.

    간격을 모르면 패널은 "마지막 실행 N시간 전"을 초록으로만 표시해, 죽은 잡을 정상으로
    오독하게 된다(적대검토 B-F7). Interval/Cron 공통으로 트리거에 직접 물어 계산한다.
    """
    try:
        now = datetime.now(KST)
        t1 = trigger.get_next_fire_time(None, now)
        if t1 is None:
            return None
        t2 = trigger.get_next_fire_time(t1, t1)
        return int((t2 - t1).total_seconds()) if t2 else None
    except Exception:  # noqa: BLE001 — 관측 보조값
        return None


async def _publish_job_registry(sched) -> None:
    """현재 등록된 잡 목록·기대 간격을 Redis에 게시하고 **유령 엔트리를 제거**한다.

    INFRA_STATUS_KEY는 해시 전체에만 TTL이 걸리는데 15초 주기 잡이 매번 갱신하므로 사실상
    영구히 만료되지 않는다 → 이관·개명으로 사라진 잡의 엔트리가 영원히 패널에 남아 "3주째
    안 도는 잡"으로 오독된다(적대검토 B-F6). 기동 시 현재 등록분만 남기고 정리한다.
    """
    try:
        import json

        import redis.asyncio as aioredis

        from erp.config import get_settings
        jobs = {j.id: _expected_interval_s(j.trigger) for j in sched.get_jobs()}
        r = aioredis.from_url(get_settings().redis_url)
        await r.set(INFRA_JOBS_KEY, json.dumps(jobs), ex=INFRA_STATUS_TTL_S)
        stale = [f.decode() if isinstance(f, bytes) else f
                 for f in await r.hkeys(INFRA_STATUS_KEY)]
        ghosts = [f for f in stale if f not in jobs]
        if ghosts:
            await r.hdel(INFRA_STATUS_KEY, *ghosts)
            logger.info("infra 잡 유령 엔트리 정리", extra={"ctx": {"removed": ghosts}})
        await r.aclose()
    except Exception as exc:  # noqa: BLE001 — 관측 기록 실패는 비차단
        logger.warning("infra 잡 레지스트리 게시 실패", extra={"ctx": {"err": str(exc)[:120]}})


_EVENT_TASKS: set = set()      # fire-and-forget 관측 태스크 강참조 보관
_RUNNING: set[str] = set()     # 지금 실행 중인 잡 — '지금 실행' 요청이 동시 1실행 제한에 조용히 버려지지 않게(v1.302)


def _on_job_event(event) -> None:
    """APScheduler 이벤트 → Redis 잡별 최종 상태(R7) — 개별 잡 수정 없이 전 잡 커버.

    대부분의 잡이 내부 try/except로 예외를 삼키므로 ERROR 이벤트는 드묾 — 상태의 1차 의미는
    '마지막으로 언제 돌았나'(중단·행업 감지)이고 error/missed는 보조 신호다.
    """
    from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED
    if event.code == EVENT_JOB_SUBMITTED:
        _RUNNING.add(event.job_id)
        return
    if event.code == EVENT_JOB_MAX_INSTANCES:
        status, err = "skipped", "이미 실행 중이라 건너뜀(동시 1실행)"
    else:
        _RUNNING.discard(event.job_id)
        status = ("missed" if event.code == EVENT_JOB_MISSED
                  else "error" if getattr(event, "exception", None) else "ok")
        err = str(getattr(event, "exception", None) or "")[:120]
    srt = getattr(event, "scheduled_run_time", None)     # 실행 이벤트(EXECUTED·ERROR·MISSED)만 가진다 — 건너뜀은 None
    sts = int(srt.timestamp()) if srt is not None else None
    try:   # AsyncIOScheduler는 이벤트를 루프 스레드에서 디스패치 — 비동기 기록으로 루프 비차단
        t = asyncio.get_running_loop().create_task(_write_infra_status(event.job_id, status, err, sts))
        _EVENT_TASKS.add(t)             # 강참조 — 참조 없는 태스크는 실행 중 GC될 수 있다(B-F8)
        t.add_done_callback(_EVENT_TASKS.discard)
    except RuntimeError:   # 루프 밖(테스트 등) — 관측 생략
        pass


async def _notify_admins(*, session_key: str, type: str, title: str, body: str) -> None:
    """전 ADMIN에게 운영 경보 알림(백업 실패·워커 이상 등). 침묵 실패 방지용(전체검토 P0).

    자체 세션·격리 — 호출부(잡)의 트랜잭션과 독립(잡 실패 경보가 잡 롤백에 휩쓸리지 않게).
    session_key는 호출 맥락 식별용(로깅)."""
    try:
        from sqlalchemy import select

        from erp.common.models import Notification, User
        async with session_factory()() as session:
            async with session.begin():
                admin_ids = (await session.execute(
                    select(User.id).where(User.role == "ADMIN",
                                          User.is_active == True))).scalars().all()  # noqa: E712
                for uid in admin_ids:
                    session.add(Notification(user_id=uid, type=type, title=title, body=body))
    except Exception as exc:  # noqa: BLE001 — 경보 자체 실패는 비차단
        logger.warning("운영 경보 알림 실패",
                       extra={"ctx": {"key": session_key, "err": str(exc)[:120]}})


async def job_fee_sync() -> None:
    """수가 마스터 동기화 — 야간 (FR-34, M0a=REST/data.go.kr). serviceKey 미설정 시 대기 모드."""
    _job_request_id("fee_sync")
    from datetime import timedelta

    from erp.config import get_settings
    from erp.integrations.fee_sync import RestFeeMasterSyncSource, run_fee_sync

    s = get_settings()
    if not s.fee_api_service_key:
        logger.info("fee_sync 대기 — FEE_API_SERVICE_KEY 미설정 (M0b 화이트리스트 등록 후 활성)")
        return
    today = kst_today()
    source = RestFeeMasterSyncSource(s.fee_api_base, s.fee_api_service_key)
    async with session_factory()() as session:
        async with session.begin():
            try:
                # 멱등키는 일자 단위 — 변경 없는 코드는 FeeSyncService가 no-op 처리
                applied = await run_fee_sync(session, source,
                                             source_version=f"api-{today.isoformat()}",
                                             since=today - timedelta(days=7))
                logger.info("fee_sync 완료", extra={"ctx": {"applied": applied}})
            except Exception as exc:  # noqa: BLE001 — 외부 호출 실패는 다음 회차 재시도
                logger.warning("fee_sync 실패", extra={"ctx": {"err": str(exc)[:120]}})


async def job_access_log_flush() -> None:
    """접속기록 Stream 소비 → audit.access_log (design §3.5 비동기 기록)."""
    _job_request_id("access_log_flush")
    try:
        import redis.asyncio as aioredis

        from erp.audit.service import ACCESS_STREAM, flush_access_stream
        from erp.config import get_settings

        r = aioredis.from_url(get_settings().redis_url)
        async with session_factory()() as session:
            async with session.begin():
                ids = await flush_access_stream(r, session)
        # 커밋 성공(위 begin 블록 종료) 후에만 스트림 삭제 — 커밋 실패 시 메시지 보존(재처리·유실 방지)
        for msg_id in ids:
            await r.xdel(ACCESS_STREAM, msg_id)
        n = len(ids)
        if n:
            logger.info("access_log flush", extra={"ctx": {"written": n}})
        await r.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("access_log flush 실패", extra={"ctx": {"err": str(exc)[:120]}})


async def job_license_expiry() -> None:
    """면허 만료 D-30/D-7 알림(인앱+이메일) + 만료자 권한 플래그 차단 (FR-04/27/47).

    로직은 LicenseService.run_expiry_alerts(정기업무 hr.license_expiry와 공용 단일 경로) —
    레지스트리 컷오버로 add_job은 제거·함수는 test 커버 유지.
    """
    _job_request_id("license_expiry")
    import asyncio

    from erp.common.email import send_email
    from erp.hr.employee_service import LicenseService

    async with session_factory()() as session:
        async with session.begin():
            out = await LicenseService().run_expiry_alerts(session, today=kst_today())
    # 이메일은 트랜잭션 커밋 후 발송(SMTP 미설정 시 no-op·실패는 비차단)
    sent = 0
    for email, title, body in out["emails"]:
        if await asyncio.to_thread(send_email, email, title, body):
            sent += 1
    logger.info("license_expiry 완료", extra={"ctx": {"blocked": out["blocked"], "emailed": sent}})


async def job_contract_lifecycle() -> None:
    """위수탁계약 만기 자동 전이 — ACTIVE→EXPIRING(D-60)·EXPIRED·auto_renew 갱신 (일 1회)."""
    _job_request_id("contract_lifecycle")
    from erp.scm.contract_service import OutsourcingContractService

    async with session_factory()() as session:
        async with session.begin():
            res = await OutsourcingContractService().run_lifecycle_transitions(session)
    logger.info("contract_lifecycle 완료", extra={"ctx": res})


async def job_appeal_deadline() -> None:
    """이의신청 기한 D-14/7/3 임박 알림 (FR-40)."""
    _job_request_id("appeal_deadline")

    from sqlalchemy import select

    from erp.claims.service import AppealService
    from erp.common.models import Notification, User

    svc = AppealService()
    async with session_factory()() as session:
        async with session.begin():
            today = kst_today()
            targets = await svc.deadline_alert_targets(session, today=today)
            if targets:
                # 의료보험팀(부서장)·관리자에게 알림 — 수신자 매핑은 부서 코드 확정 시 보강
                admin_ids = (await session.execute(
                    select(User.id).where(User.role.in_(["ADMIN", "MANAGER"])))).scalars().all()
                for appeal in targets:
                    d_day = (appeal.legal_deadline - today).days
                    for uid in admin_ids:
                        session.add(Notification(
                            user_id=uid, type="APPEAL_DEADLINE",
                            title=f"이의신청 기한 임박 D-{d_day}",
                            body=f"appeal #{appeal.id} — 기한 {appeal.legal_deadline}",
                            href="/claims"))   # X2-3 딥링크
    logger.info("appeal_deadline 완료", extra={"ctx": {"targets": len(targets)}})


async def job_attendance_summary() -> None:
    """월 근태 집계 (FR-29) — 그룹웨어(Clinic) REST 수집(M0a=REST) → staging → 집계.

    CLINIC_API_KEY 미설정 시 수집은 건너뛰고 잔여 staging 소비만 수행(대기 모드)."""
    _job_request_id("attendance_summary")
    from datetime import datetime, timedelta

    from erp.config import get_settings
    from erp.hr.attendance import consume_attendance_staging
    from erp.integrations.groupware import (
        GroupwareAttendanceCollector, RestGroupwareAttendanceSource,
    )

    s = get_settings()
    async with session_factory()() as session:
        async with session.begin():
            if s.clinic_api_key:
                source = RestGroupwareAttendanceSource(
                    s.clinic_api_base, s.clinic_api_key, s.hospital_code)
                since = datetime.now().astimezone() - timedelta(days=1)  # 일배치 — 전일분
                try:
                    got = await GroupwareAttendanceCollector(source).collect(session, since=since)
                    logger.info("그룹웨어 근태 수집", extra={"ctx": got})
                except Exception as exc:  # noqa: BLE001 — 수집 실패해도 잔여 소비는 진행
                    import httpx
                    code = (exc.response.status_code
                            if isinstance(exc, httpx.HTTPStatusError) else None)
                    if code in (404, 405):
                        # 원인과 조치를 이름으로 — Clinic의 이 경로는 POST(기록 쓰기)만 있다.
                        # 일시 장애가 아니라 구조적 부재라 재시도로 풀리지 않는다.
                        logger.warning(
                            "그룹웨어 근태 수집 불가 — Clinic에 근태 조회(GET) EP가 없음. "
                            "원천 결정 필요: /admin/decisions 'clinic-attendance-source'",
                            extra={"ctx": {"http": code}})
                    else:
                        logger.warning("그룹웨어 근태 수집 실패",
                                       extra={"ctx": {"err": str(exc)[:120]}})
            else:
                logger.info("CLINIC_API_KEY 미설정 — 수집 생략, staging 소비만 수행")
            result = await consume_attendance_staging(session)
    logger.info("attendance_summary 완료", extra={"ctx": result})


async def job_clinic_staff_sync() -> None:
    """Clinic 직원 동기화 (FR-28) — /api/clinic/his/staff → hr.employees. 키 미설정 시 생략."""
    _job_request_id("clinic_staff_sync")
    from erp.config import get_settings
    from erp.hr.staff_sync import consume_clinic_staff
    from erp.integrations.clinic_staff import RestClinicStaffSource

    s = get_settings()
    if not s.clinic_api_key:
        logger.info("clinic_staff_sync 대기 — CLINIC_API_KEY 미설정")
        return
    source = RestClinicStaffSource(s.clinic_api_base, s.clinic_api_key, s.hospital_code)
    async with session_factory()() as session:
        async with session.begin():
            try:
                result = await consume_clinic_staff(session, source)
                logger.info("clinic_staff_sync 완료", extra={"ctx": result})
                # 경계 통과 감사(Phase0 G3) — 직원 마스터 PII pull 불변 트레일
                from erp.audit.service import AuditService
                await AuditService().record_audit(
                    session, actor=None, action="SYNC",
                    entity="integrations.clinic_staff_sync", after=result)
            except Exception as exc:  # noqa: BLE001 — 외부 호출 실패는 다음 회차 재시도
                logger.warning("clinic_staff_sync 실패", extra={"ctx": {"err": str(exc)[:120]}})


async def job_backup() -> None:
    """일 백업 22:00 — ERP DB 전체 pg_dump + 첨부 tar + AES-GCM 암호화 + 세대관리 (design §6.5)."""
    _job_request_id("backup")
    import asyncio
    import os

    from erp.common.backup import (
        attachments_backup_path,
        pg_dump,
        push_offsite,
        run_backup,
        tar_dir,
    )
    from erp.config import get_settings

    s = get_settings()
    if not s.backup_dir:
        logger.info("backup 대기 — BACKUP_DIR 미설정")
        return
    key = s.backup_enc_key or s.data_enc_key
    db_url = s.database_url
    try:
        # pg_dump·암호화는 블로킹 — 스레드로 (이벤트루프 비점유).
        # 2026-10-09 전체 흐름 검토 2차 H: 평문을 메모리에 들지 않게 workdir(0700 임시 디렉터리)로
        # 파일 스트리밍 → 청크 암호화(v2). 종전 bytes 반환은 1G 한도에서 OOM·2GiB 초과 시 OverflowError.
        today_iso = kst_today().isoformat()
        path = await asyncio.to_thread(
            run_backup, backup_dir=s.backup_dir, enc_key_hex=key,
            today_iso=today_iso, keep_daily=s.backup_keep_daily,
            dump_fn=lambda workdir: pg_dump(db_url, out_dir=workdir),
            attachments_fn=lambda workdir: tar_dir(s.attachment_dir, out_dir=workdir))  # 첨부 볼륨
        logger.info("backup 완료", extra={"ctx": {"path": path}})
        # 오프사이트 전송(재해복구 P2-1) — 목적지 설정 시. 로컬 백업은 성공했으므로 전송 실패는
        # 별도 경보(로컬본은 보존)·잡 비차단. 미설정 시 현행(로컬만).
        # 2026-10-09 전체 흐름 검토 2차 H: 종전엔 DB 파일만 보내 첨부(서명본·증빙)는 오프사이트에 없었다.
        if s.backup_offsite_dest:
            att_path = attachments_backup_path(s.backup_dir, today_iso)
            targets = [path] + ([att_path] if os.path.exists(att_path) else [])
            try:
                for target in targets:
                    await asyncio.to_thread(
                        push_offsite, target, dest=s.backup_offsite_dest,
                        ssh_port=s.backup_offsite_ssh_port, ssh_key=s.backup_offsite_ssh_key)
            except Exception as exc:  # noqa: BLE001 — 전송 실패는 로컬본 보존·경보만
                logger.error("오프사이트 백업 전송 실패", extra={"ctx": {"err": str(exc)[:120]}})
                await _notify_admins(session_key="backup_offsite_fail",
                                     type="BACKUP_OFFSITE_FAILURE",
                                     title="오프사이트 백업 전송 실패 — 확인 필요",
                                     body=f"로컬 백업은 성공·원격 전송 실패: {str(exc)[:200]}")
    except Exception as exc:  # noqa: BLE001 — 백업 실패는 잡 죽이지 않음(다음 회차)
        # 침묵 실패 방지 — 로그만으론 수 주간 백업 0개여도 인지 불가(전체검토 P0-1).
        logger.error("backup 실패", extra={"ctx": {"err": str(exc)[:120]}})
        await _notify_admins(session_key="backup_fail", type="BACKUP_FAILURE",
                             title="일 백업 실패 — 확인 필요",
                             body=f"pg_dump/암호화 실패: {str(exc)[:200]} — 디스크·PG버전 확인")


async def job_heartbeat() -> None:
    """worker-batch 생존 하트비트 — Redis 키 갱신(TTL). 딥 헬스(/api/health/deep)가 나이 검사해
    행업/사망 워커를 감지(컨테이너는 살아있어 docker restart 미발동·P0-2)."""
    try:
        import redis.asyncio as aioredis

        from erp.config import get_settings
        r = aioredis.from_url(get_settings().redis_url)
        await r.set(HEARTBEAT_KEY, str(int(time.time())), ex=HEARTBEAT_TTL_S)
        await r.aclose()
    except Exception as exc:  # noqa: BLE001 — 하트비트 실패는 비차단(다음 회차)
        logger.warning("heartbeat 실패", extra={"ctx": {"err": str(exc)[:120]}})


_PGWATCH_KEY = "erp:pgwatch:alerted"       # 이웃감시 경보 디둡(쿨다운 내 재알림 억제)
_PGWATCH_COOLDOWN_S = 3600                  # 임계 지속 시 1시간당 최대 1회 경보


async def job_pg_neighbor_watch() -> None:
    """공유 PG(HIS·OpenPACS 동거) 슬롯 점유 이웃 감시 — 임계 초과 시 ADMIN 경보(P2-4).

    OpenPACS 유휴 누수→슬롯 고갈→HIS SSO 500 인시던트의 조기경보를 온디맨드 진단(관리자가 눌러야
    보임)에서 능동 감시로. Redis 디둡으로 임계 지속 중 매 회차 스팸 방지, 회복 시 키 삭제→재경보 즉시.
    """
    _job_request_id("pg_neighbor_watch")
    from erp.app.db_diagnostics_service import shared_pg_stats

    try:
        async with session_factory()() as session:
            stats = await shared_pg_stats(session)
        if not stats or stats.get("usage_pct") is None:
            return       # sqlite·통계 불가 → 조용히 스킵

        import redis.asyncio as aioredis

        from erp.config import get_settings
        r = aioredis.from_url(get_settings().redis_url)
        try:
            if stats["warning"]:
                fresh = await r.set(_PGWATCH_KEY, str(stats["usage_pct"]),
                                    ex=_PGWATCH_COOLDOWN_S, nx=True)
                if fresh:    # 쿨다운 내 최초 breach만 알림
                    top = ", ".join(f"{t['usename']}:{t['connections']}"
                                    for t in stats["by_tenant"][:4])
                    await _notify_admins(
                        session_key="pg_neighbor_watch", type="PG_SLOT_PRESSURE",
                        title=f"공유 PG 슬롯 점유 {stats['usage_pct']}% "
                              f"(임계 {stats['warn_pct']}%)",
                        body=f"총 {stats['total']}/{stats['max_connections']} 연결 — "
                             f"테넌트 {top}. OpenPACS 유휴 누수 의심 시 ops-runbook §9.1 "
                             f"(회복=superuser·HIS팀).")
                    logger.warning("pg_neighbor_watch 경보",
                                   extra={"ctx": {"usage_pct": stats["usage_pct"],
                                                  "total": stats["total"]}})
            else:
                await r.delete(_PGWATCH_KEY)     # 회복 → 다음 breach 즉시 재경보
        finally:
            await r.aclose()
    except Exception as exc:  # noqa: BLE001 — 감시 실패는 비차단(다음 회차)
        logger.warning("pg_neighbor_watch 실패", extra={"ctx": {"err": str(exc)[:120]}})


# retention_sweep·cost_review(월간)는 정기업무 레지스트리로 컷오버 — recurring_handlers의
# common.retention_sweep·itam.cost_review를 job_recurring이 실행(월경계 재시작 보정도 러너
# catchup+기간멱등이 담당 → _startup_catchup 제거).


async def job_collect_drops() -> None:
    """파일 드롭 수집 (M0a fallback) — 드롭 디렉터리 → 스테이징."""
    _job_request_id("collect_drops")
    from erp.integrations.file_drop import FileDropCollector

    collector = FileDropCollector()
    async with session_factory()() as session:
        async with session.begin():
            await collector.collect(session)


async def job_settlement_callbacks() -> None:
    """검진권 정산 지급 회신 송신 (ERP→HIS settlement.paid, DB-4) — 아웃박스 QUEUED 전송·재시도."""
    _job_request_id("settlement_callbacks")
    from erp.integrations.settlement_callback import send_pending_callbacks

    async with session_factory()() as session:
        async with session.begin():
            out = await send_pending_callbacks(session)
    if out.get("sent") or out.get("failed"):
        logger.info("settlement_callbacks", extra={"ctx": out})


async def job_patient_accrual() -> None:
    """환자 본인부담 발생주의 미수금 발생 — 당월·전월 진료분(원무 심화). 게이트 OFF면 no-op."""
    _job_request_id("patient_accrual")
    from datetime import timedelta

    from erp.config import get_settings
    from erp.patient.receivable_service import PatientReceivableService

    if not get_settings().patient_accrual_enabled:
        return
    today = kst_today()
    prev = today.replace(day=1) - timedelta(days=1)
    svc = PatientReceivableService()
    async with session_factory()() as session:
        async with session.begin():
            for ym in (today.strftime("%Y-%m"), prev.strftime("%Y-%m")):
                out = await svc.accrue_all(session, care_month=ym)
                if out.get("posted"):
                    logger.info("patient_accrual", extra={"ctx": out})


async def job_budget_alerts() -> None:
    """예산 초과 능동 경보 — budget_execution이 OVER/WARNING을 계산해도 조회 시만 노출되던 갭
    (내부운영 갭 감사 Tier2 quick win). OVER 존재 시 재무 역할(ADMIN·DIRECTOR·FINANCE_MANAGER)
    알림. 미확인(read_at NULL) 동일 경보가 있으면 재발화 생략(일일 반복 소음 방지·디둡)."""
    _job_request_id("budget_alerts")
    from sqlalchemy import select

    from erp.common.models import Notification, User
    from erp.finance.report_service import FinanceReportService
    year = kst_today().year
    async with session_factory()() as session:
        async with session.begin():
            rows = await FinanceReportService().budget_execution(session, fiscal_year=year)
            over = [r for r in rows if r["status"] == "OVER"]
            warning = [r for r in rows if r["status"] == "WARNING"]
            if not over:
                return
            top = sorted(over, key=lambda r: r["available"])[:5]
            detail = " · ".join(
                f"부서{r['department_id']} {r['account_code']} 초과 {-r['available']:,.0f}원"
                for r in top)
            body = (f"{year}년 예산 초과 {len(over)}건(경고 {len(warning)}건) — {detail}"
                    + (" 외" if len(over) > 5 else ""))
            recipients = (await session.execute(
                select(User.id).where(User.role.in_(
                    ("ADMIN", "DIRECTOR", "FINANCE_MANAGER"))))).scalars().all()
            added = 0
            for uid in recipients:
                dup = await session.scalar(select(Notification.id).where(
                    Notification.user_id == uid, Notification.type == "BUDGET_OVER",
                    Notification.read_at.is_(None)))
                if dup:
                    continue
                session.add(Notification(user_id=uid, type="BUDGET_OVER",
                                         title=f"예산 초과 경보 — {len(over)}건",
                                         body=body, href="/finance"))
                added += 1
            if added:
                logger.info("budget_alerts", extra={"ctx": {"over": len(over),
                                                            "notified": added}})


async def job_annual_leave_grant() -> None:
    """재직 전원 당해연도 연차 자동 부여 (근기법 §60·멱등) — 미부여 재직자만 부여.

    `grant_annual`이 미배선이라 LeaveGrant가 없으면 모든 연차 신청이 거부되던 결함 해소. 매일 실행하나
    미부여자만 단일 쿼리로 추려 저비용(정상 상태=0건). 연초·신규입사 모두 반영."""
    _job_request_id("annual_leave_grant")
    from erp.hr.leave_service import LeaveService
    async with session_factory()() as session:
        async with session.begin():
            out = await LeaveService().grant_annual_all(session, year=kst_today().year)
    if out.get("granted"):
        logger.info("annual_leave_grant", extra={"ctx": out})


async def job_esign_fetch() -> None:
    """서명 완료본 회수 아웃박스 드레인 (e-sign #6) — 인박스와 분리된 별도 트랜잭션(HIS 지연 격리)."""
    _job_request_id("esign_fetch")
    from erp.integrations.esign_fetch import process_esign_fetch_queue

    async with session_factory()() as session:
        async with session.begin():
            out = await process_esign_fetch_queue(session)
    if out.get("done") or out.get("failed"):
        logger.info("esign_fetch", extra={"ctx": out})


async def job_his_sync() -> None:
    """HIS 환자·수납 staging 잔여 소비 (환자→수납).

    ⚠ 능동 수집(`/api/v1/erp/patients|payments` 폴링)은 제거됨 — HIS가 해당 ERP-sync EP를
    제공하지 않음으로 확정(수납=`/integration/billing/lines` 재사용, 환자 디렉터리 실수요 없음,
    회신서 §B). 가정 능동수집 어댑터(구 integrations/his.py)는 2026-06-30 삭제. 본 잡은 스테이징
    →코어 소비만 남아 기존 NEW staging 잔여를 멱등 드레인하며, 향후 HIS가 스테이징으로 push하면
    그대로 재배선 타깃이 된다(live 환자/수납 적재는 his_billing.backfill_lines가 별도 담당).

    환자 소비를 수납보다 먼저 수행(수납 차트번호 해소 선결)."""
    _job_request_id("his_sync")
    from erp.patient.ingest import consume_patient_staging, consume_payment_staging

    async with session_factory()() as session:
        async with session.begin():
            pat = await consume_patient_staging(session)   # 환자 선행
            pay = await consume_payment_staging(session)
    if any(pat.values()) or any(pay.values()):
        logger.info("his_sync 완료", extra={"ctx": {"patients": pat, "payments": pay}})


async def job_parse_notices() -> None:
    """통보서 스테이징 파싱 (FR-38) — G1: 한정자 잠정값, 실물 검증 전 결과는 검토용."""
    _job_request_id("parse_notices")
    from erp.claims.ingest_service import NoticeIngestService

    svc = NoticeIngestService()
    async with session_factory()() as session:
        async with session.begin():
            result = await svc.process_pending(session)
    if any(result.values()):
        logger.info("parse_notices 완료", extra={"ctx": result})


async def job_sync_verify() -> None:
    """주 1회 AI 정합성 대사 — 일요일 03:00 (스펙 v1.0 ⑥, ERP push)."""
    _job_request_id("sync_verify")
    logger.info("sync_verify 시작 — publisher 설정 시 활성화")
    # TODO(B-3 화이트리스트 등록 후): 전체 {erp_ref_id, revision_no, content_hash} push


# refresh_bi_marts는 정기업무 레지스트리로 컷오버 — 일괄 갱신 로직은 erp.analytics.marts.
# refresh_all_marts(마트별 세이브포인트 격리 유지), 실행은 analytics.bi_marts 핸들러(job_recurring).


async def job_balance_checkpoint() -> None:
    """계정 잔액 체크포인트 적재 + 자기검증(성능 H-2) — balance_sheet의 전 원장 스캔을 체크포인트+델타로.

    전월말 경계까지 증분 적재(O(당기)) 후, 오늘 기준 체크포인트+델타 == 전구간 스캔을 대조(정확성 상시
    감시). 불일치 시 ADMIN 경보(체크포인트 오류=재무상태표 왜곡 위험). 배치라 검증 full-scan은 off-peak 허용."""
    _job_request_id("balance_checkpoint")
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo

    from sqlalchemy import select

    from erp.common.models import Notification, User
    from erp.finance.statements import StatementService
    ss = StatementService()
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    prev_end = date(today.year, today.month, 1) - timedelta(days=1)   # 전월 말일(안정 경계)
    async with session_factory()() as session:
        async with session.begin():
            cp = await ss.refresh_balance_checkpoint(session, through=prev_end)
            chk = await ss.verify_balance_checkpoint(session, as_of=today)
            logger.info("balance_checkpoint", extra={"ctx": {**cp, "verify": chk}})
            if not chk["ok"]:
                logger.error("잔액 체크포인트 불일치 — 재무상태표 왜곡 위험", extra={"ctx": chk})
                for uid in (await session.execute(
                        select(User.id).where(User.role == "ADMIN"))).scalars().all():
                    session.add(Notification(
                        user_id=uid, type="BALANCE_CHECKPOINT_MISMATCH",
                        title="잔액 체크포인트 불일치 — 확인 필요",
                        body=f"체크포인트+델타≠전구간 (상이 계정 {chk.get('mismatched')}) — 재계산 필요",
                        href="/finance-statements"))


async def job_inbound_consume() -> None:
    """HIS 웹훅 인박스 소비 (마스터플랜 Wave) — his_inbound_events(NEW) → 도메인 모델. interval 5분.

    소비 후 진료라인 변경분(차트×월)을 **이벤트 기반 표적 사전심사**로 자동 재평가
    (HIS 청구 발생 → 수 분 내 자동 적발·P1 자동 이상없음). 별도 Tx로 격리·멱등.
    """
    _job_request_id("inbound_consume")
    from erp.claims.worklist_service import WorklistService
    from erp.integrations.inbound_consumer import InboundConsumer

    async with session_factory()() as session:
        async with session.begin():
            await _refresh_statutory(session)
            out = await InboundConsumer().consume(session)
    if out["processed"]:
        logger.info("inbound 소비", extra={"ctx": {k: v for k, v in out.items()
                                                   if k != "prereview_targets"}})
    for chart_no, care_month in out.get("prereview_targets", []):   # 이벤트 기반 표적 재평가
        async with session_factory()() as session:
            async with session.begin():
                await WorklistService().sync_from_billing_lines(
                    session, actor=0, care_month=care_month, chart_no=chart_no)


async def job_prereview_sync() -> None:
    """사전심사 일일 전체 sync — 당월·전월 (이벤트 누락 안전망 + 기존 데이터 백필 + 룰/매핑 변경 반영).

    멱등(save_run) — 이벤트 표적 재평가와 중복돼도 안전. 진료시간 외 새벽 실행.
    """
    _job_request_id("prereview_sync")
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from erp.claims.worklist_service import WorklistService
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    prev = (now.year - (now.month == 1), (now.month - 2) % 12 + 1)
    months = [now.strftime("%Y-%m"), f"{prev[0]:04d}-{prev[1]:02d}"]
    for ym in months:
        async with session_factory()() as session:
            async with session.begin():
                out = await WorklistService().sync_from_billing_lines(session, actor=0, care_month=ym)
        logger.info("사전심사 일일 sync", extra={"ctx": {"month": ym, "lines": out.get("lines"),
                    "auto_cleared": out.get("auto_cleared"), "auto_dismissed": out.get("auto_dismissed")}})


async def job_ledger_integrity() -> None:
    """일 1회 원장 무결성(G11) — voucher_lines 해시 체인 증분 체크포인트 + 전체 검증.

    검증 실패(트리거 우회 등 변조 의심) 시 ADMIN 알림. 라인은 append-only라 증분 해시로 저비용."""
    _job_request_id("ledger_integrity")
    from sqlalchemy import select

    from erp.common.models import Notification, User
    from erp.finance.ledger_integrity_service import LedgerIntegrityService
    svc = LedgerIntegrityService()
    async with session_factory()() as session:
        async with session.begin():
            cp = await svc.checkpoint(session, actor=0)
            chk = await svc.verify(session)
            logger.info("ledger_integrity", extra={"ctx": {"cp": cp, "verify": chk}})
            if not chk["ok"]:
                admin_ids = (await session.execute(
                    select(User.id).where(User.role == "ADMIN"))).scalars().all()
                for uid in admin_ids:
                    session.add(Notification(
                        user_id=uid, type="LEDGER_INTEGRITY",
                        title="원장 무결성 경보 — 변조 의심",
                        body=f"체크포인트 #{chk.get('broken_checkpoint_id')} 재계산 불일치 — 즉시 확인 필요",
                        href="/finance-periods"))


async def job_approval_ledger_verify() -> None:
    """일 1회 결재 불변 원장(해시체인) 전 구간 검증 — 변조(값 수정·행 삭제·순서 교란) 시 ADMIN 알림.

    체인은 DB append-only(REVOKE·0203)이나, 이를 우회한 superuser 변조까지 사후 탐지하는 상시
    감시(온디맨드 verify EP의 배치 짝 — ledger_integrity 동형)."""
    _job_request_id("approval_ledger_verify")
    from sqlalchemy import select

    from erp.common.approval_ledger import ApprovalLedgerService
    from erp.common.models import Notification, User
    async with session_factory()() as session:
        async with session.begin():
            svc = ApprovalLedgerService()
            chk = await svc.verify(session)
            if chk["ok"]:
                # 검증 통과 → 현재 head를 앵커로 봉인(이후 꼬리 절단 탐지 기준점). head_hash를
                # 로그에 남겨 외부 관측(별도 채널 봉인의 최소 형태·HIGH-1/2 완화).
                cp = await svc.checkpoint(session)
                logger.info("approval_ledger_verify", extra={"ctx": {**chk, "checkpoint": cp}})
            else:
                logger.error("결재 원장 변조 의심", extra={"ctx": chk})
                # 수신자 = ADMIN + DIRECTOR — ADMIN 부재이거나 ADMIN 본인이 변조 행위자여도 경보가
                # 도달하도록 이중화(적대검토 LOW — 탐지 알림 사각).
                recipients = (await session.execute(
                    select(User.id).where(User.role.in_(("ADMIN", "DIRECTOR"))))).scalars().all()
                seq = chk.get("first_broken_seq")
                body = (f"해시체인 seq {seq} 파손({chk.get('reason')}) — 즉시 확인 필요")
                for uid in recipients:
                    # 디둡 — 같은 파손 지점에 대한 미확인(read_at NULL) 경보가 이미 있으면 재발화 생략
                    # (매일 배치가 동일 알림을 반복 생성하던 소음 방지).
                    dup = await session.scalar(select(Notification.id).where(
                        Notification.user_id == uid,
                        Notification.type == "APPROVAL_LEDGER_TAMPER",
                        Notification.read_at.is_(None),
                        Notification.body == body).limit(1))
                    if dup is None:
                        session.add(Notification(
                            user_id=uid, type="APPROVAL_LEDGER_TAMPER",
                            title="결재 원장 무결성 경보 — 변조 의심",
                            body=body, href="/approvals"))


async def _refresh_statutory(session) -> None:
    """워커는 API 세션 훅을 거치지 않는다 — 법정 파라미터 관리화면 등록분을 진입점에서 반영(G8).

    지금 워커 경로엔 요율 계산이 없지만(검토 실측), 생기는 순간 코드 상수가 조용히 쓰이지 않게 한다.
    """
    from erp.hr import statutory
    if statutory._stale():
        try:
            async with session.begin_nested():     # 실패해도 바깥 트랜잭션(잡)을 오염시키지 않게
                await statutory.refresh_db_params(session)
        except Exception as exc:  # noqa: BLE001 — 갱신 실패가 잡을 막지 않는다
            logger.warning("법정 파라미터 갱신 실패", extra={"ctx": {"err": str(exc)[:120]}})


async def job_recurring() -> None:
    """정기업무(주/월/분기) 메타잡 — enabled AUTO 업무를 claim 후 실행(잡별 커밋)·MANUAL은 물질화.

    컷오버 완료(v1.247.0): dry_run=False로 실 실행. 이관된 AUTO 4종(contract_lifecycle·
    annual_leave_grant·patient_accrual·prereview_sync)의 APScheduler 엔트리는 제거됨 — 본 메타잡이
    유일 실행 경로(이중실행 없음). catchup=1로 무영속 재시작 시 직전 기간 소급(created_at 게이팅).
    리더(advisory lock)에서만 발화. AI_ASSISTED는 **초안 준비(auto_prepare 기본 True)**와 확정된
    결정 반영(sync)을 여기서 한다 — 사람이 화면에서 버튼을 누르지 않아도 초안이 대기함에 올라온다.
    기획서 R10 컷오버.
    """
    from erp.app.recurring_service import RecurringRunner
    async with session_factory()() as session:
        await _refresh_statutory(session)
        await session.commit()
        summary = await RecurringRunner().run_due(session, dry_run=False, catchup=1,
                                                  triggered_by="SCHEDULER")
    if (summary.get("materialized") or summary.get("executed") or summary.get("ai_resolved")
            or summary.get("prepared")):
        logger.info("정기업무 메타잡", extra={"ctx": summary})


async def job_batch_control(sched) -> None:
    """배치 작업 화면의 지금 실행·일시정지·재개 명령 반영 + 잡 일정(주기·다음 실행) 게시 — 30초(리더 전용)."""
    import redis.asyncio as aioredis

    from erp.common import batch_jobs
    from erp.config import get_settings
    r = aioredis.from_url(get_settings().redis_url)
    try:
        fixed = await batch_jobs.reconcile_paused(r, sched)     # 기동 시 재적용 실패 등 — 기록과 실제를 매 주기 맞춤
        if fixed:
            logger.warning("일시정지 기록과 달리 돌던 잡을 멈춤", extra={"ctx": {"jobs": fixed}})
        done = await batch_jobs.apply_commands(r, sched, running=set(_RUNNING))
        await batch_jobs.publish_schedule(r, sched)
        if done:
            logger.info("배치 조작 반영", extra={"ctx": {"results": done}})
    except Exception as exc:
        # 삼키지 않는다 — 이 잡의 상태가 '실패'로 남아 화면·결정·조치함에 드러나게(종전엔 늘 정상으로 보였다)
        logger.warning("배치 조작 반영 실패", extra={"ctx": {"err": str(exc)[:120]}})
        raise
    finally:
        await r.aclose()


def build_scheduler() -> AsyncIOScheduler:
    # P1-5 신뢰성 — misfire 유실 방지(5분 유예)·중첩 합침(coalesce)·동시 1인스턴스(중복실행 차단)
    sched = AsyncIOScheduler(timezone="Asia/Seoul", job_defaults={
        "misfire_grace_time": 300, "coalesce": True, "max_instances": 1})
    # 진료시간(08–18) 외 — design §2.3 제약
    sched.add_job(job_fee_sync, CronTrigger(hour=22, minute=30), id="fee_sync")
    # license_expiry는 정기업무 레지스트리 컷오버(hr.license_expiry·이메일=러너 post-commit 채널)
    sched.add_job(job_appeal_deadline, CronTrigger(hour=5, minute=30), id="appeal_deadline")
    sched.add_job(job_attendance_summary, CronTrigger(hour=4, minute=0), id="attendance_summary")
    # ⚠ contract_lifecycle·annual_leave_grant·patient_accrual·prereview_sync은 정기업무 레지스트리로
    #   컷오버(v1.247.0) — job_recurring 메타잡이 실행. 여기 add_job 제거(이중실행 방지).
    sched.add_job(job_clinic_staff_sync, CronTrigger(hour=3, minute=30), id="clinic_staff_sync")
    sched.add_job(job_backup, CronTrigger(hour=22, minute=0), id="backup")
    sched.add_job(job_heartbeat, "interval", seconds=60, id="heartbeat")
    sched.add_job(job_pg_neighbor_watch, "interval", minutes=10, id="pg_neighbor_watch")
    # Phase0 G5 — 공유 Redis 장애 시 유실창(xadd~flush)을 1분→15초로 축소(접속기록 내구성)
    sched.add_job(job_access_log_flush, "interval", seconds=15, id="access_log_flush")
    sched.add_job(job_parse_notices, "interval", minutes=10, id="parse_notices")
    sched.add_job(job_collect_drops, "interval", minutes=10, id="collect_drops")
    sched.add_job(job_his_sync, "interval", minutes=10, id="his_sync")
    sched.add_job(job_inbound_consume, "interval", minutes=5, id="inbound_consume")
    sched.add_job(job_settlement_callbacks, "interval", minutes=2, id="settlement_callbacks")
    sched.add_job(job_esign_fetch, "interval", minutes=2, id="esign_fetch")
    # 정기업무 레지스트리 컷오버(job_recurring 실행): balance_checkpoint·ledger_integrity(v1.251)·
    #   budget_alerts·approval_ledger_verify(v1.252)·cost_review·retention_sweep·license_expiry·
    #   bi_marts(v1.253~1.255). 테스트 커버 잡 함수만 유지·add_job 제거.
    sched.add_job(job_sync_verify, CronTrigger(day_of_week="sun", hour=3), id="sync_verify")
    # 정기업무 메타잡 — enabled AUTO 실행 + MANUAL 물질화 + AI 결정 반영. 매시 :20(리더 전용).
    sched.add_job(job_recurring, CronTrigger(minute=20), id="recurring")
    # 배치 작업 화면(v1.302) — 명령 반영·일정 게시(리더 전용·30초)
    sched.add_job(job_batch_control, "interval", seconds=30, id="batch_control", args=[sched])
    return sched


async def inbound_listener() -> None:
    """완전 실시간 — Redis 구독으로 웹훅 수신 즉시 소비(+표적 사전심사). 폴링(5분)은 안전망 잔존.

    소비는 NEW 전건을 처리하므로 신호 폭주는 자연 합쳐짐(순차 처리·멱등). get_message(timeout)
    폴링이라 무메시지 대기는 정상(재구독 churn 없음)·health_check로 연결 유지·끊기면 재구독.
    """
    import redis.asyncio as aioredis

    from erp.common.redis_client import INBOUND_SIGNAL_CHANNEL
    from erp.config import get_settings
    while True:
        try:
            client = aioredis.from_url(get_settings().redis_url, health_check_interval=30)
            pubsub = client.pubsub()
            await pubsub.subscribe(INBOUND_SIGNAL_CHANNEL)
            logger.info("실시간 인박스 리스너 구독", extra={"ctx": {"ch": INBOUND_SIGNAL_CHANNEL}})
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30)
                if msg is None:
                    continue                          # 무메시지 타임아웃 — 정상, 계속 대기
                try:
                    await job_inbound_consume()        # 소비 + 이벤트 기반 표적 사전심사
                except Exception as exc:  # noqa: BLE001 — 리스너 상주 유지
                    logger.warning("실시간 소비 실패", extra={"ctx": {"err": str(exc)[:120]}})
        except Exception as exc:  # noqa: BLE001 — 연결 끊김 등 → 재구독
            logger.warning("인박스 리스너 재연결", extra={"ctx": {"err": str(exc)[:120]}})
            await asyncio.sleep(5)


# 스케줄러 단일 인스턴스 보장용 pg advisory lock 키(64bit 상수). worker-batch를 실수로 2+
# 레플리카로 띄워도 리더 1개만 잡을 발화(cron 이중발화·백업 pg_dump 경합·중복 알림 차단).
_SCHED_LOCK_KEY = 8_270_364_501_027


async def _acquire_scheduler_lock():
    """pg_try_advisory_lock으로 리더 선출. 반환: 락을 쥔 커넥션(프로세스 생애 유지 — 닫으면 세션
    락 해제) 또는 None(타 인스턴스 보유). MemoryJobStore·무분산락 구조에서 이중발화 방지."""
    from sqlalchemy import text

    from erp.db import get_engine
    conn = await get_engine().connect()
    got = await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": _SCHED_LOCK_KEY})
    # 세션 advisory lock은 커밋 뒤에도 유지된다 — 커밋해 트랜잭션을 닫는다. 2026-10-10 prod: 이 커넥션이 autobegin
    # 트랜잭션을 프로세스 생애 내내 열어 두어(idle in transaction·backend_xmin 고정) **공유 PG 전체의 VACUUM
    # 정리 경계**를 붙잡았다(HIS·OpenPACS DB까지 dead tuple 미회수). 다음 배포까지 수 주 지속될 수 있었다.
    await conn.commit()
    if not got:
        await conn.close()
        return None
    return conn


async def _watch_scheduler_lock(conn, *, interval: float = 60.0) -> None:
    """리더 락 커넥션 생존 감시 — 끊기면(공유 PG 재시작 등) 세션 락도 사라져 다른 인스턴스가 리더가 될 수 있다.

    감지 즉시 프로세스를 끝내 컨테이너 재시작(restart: unless-stopped)으로 락을 다시 잡게 한다. 질의 뒤 커밋해
    트랜잭션을 열어 두지 않는다(2026-10-10 — 열린 트랜잭션이 공유 PG VACUUM 경계를 붙잡았던 결함의 재발 방지).
    """
    import os

    from sqlalchemy import text
    while True:
        await asyncio.sleep(interval)
        try:
            await conn.scalar(text("SELECT 1"))
            await conn.commit()
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 락 상실로 본다
            logger.error("스케줄러 리더 락 커넥션 상실 — 재시작해 락을 다시 잡습니다",
                         extra={"ctx": {"err": str(exc)[:120]}})
            os._exit(1)


async def main() -> None:
    setup_logging()
    assert_process_tz_kst()   # naive KST → timestamptz 쓰기 정합의 전제(불일치=기동 중단)
    # 리더 선출 — 락 미획득(타 worker-batch 실행 중)이면 잡 미발화로 상주(이중발화 방지·재시작 루프 회피)
    lock_conn = await _acquire_scheduler_lock()
    if lock_conn is None:
        logger.error("스케줄러 advisory lock 미획득 — 다른 worker-batch 인스턴스 실행 중. "
                     "잡 발화 생략(상주).")
        await asyncio.Event().wait()
        return
    logger.info("스케줄러 리더 — advisory lock 획득")
    # 고아 RUNNING 회수(B-F1) — 리더 교체 = 이전 보유자 소멸이므로 남은 RUNNING은 정의상 고아.
    # 회수하지 않으면 그 기간이 영구 미실행 + stuck_running 신호가 상시 503으로 고착된다.
    try:
        from erp.app.recurring_service import RecurringRunner as _Runner
        async with session_factory()() as _s:
            got = await _Runner().reclaim_orphan_runs(_s)
        if got["reclaimed"]:
            logger.warning("정기업무 고아 실행 회수", extra={"ctx": got})
    except Exception as exc:                          # 회수 실패가 기동을 막지는 않음
        logger.error("고아 RUNNING 회수 실패", extra={"ctx": {"error": str(exc)[:120]}})
    sched = build_scheduler()
    # R7 관측성 — 잡 실행/오류/미스파이어를 Redis에 기록(관리화면 인프라잡 패널이 조회)
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
    from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_SUBMITTED
    sched.add_listener(_on_job_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED
                       | EVENT_JOB_SUBMITTED | EVENT_JOB_MAX_INSTANCES)
    sched.start()
    await _publish_job_registry(sched)         # 등록 잡·기대 간격 게시 + 유령 엔트리 정리(B-F6)
    try:   # 배치 작업 화면에서 일시정지한 잡은 재시작 후에도 멈춰 있게 + 일정 즉시 게시
        import redis.asyncio as aioredis

        from erp.common import batch_jobs
        from erp.config import get_settings
        _r = aioredis.from_url(get_settings().redis_url)
        paused = await batch_jobs.reapply_paused(_r, sched)
        await batch_jobs.publish_schedule(_r, sched)
        await _r.aclose()
        if paused:
            logger.warning("일시정지 잡 재적용", extra={"ctx": {"paused": paused}})
    except Exception as exc:  # noqa: BLE001 — Redis 불가 시에도 기동
        logger.error("일시정지 재적용 실패", extra={"ctx": {"err": str(exc)[:120]}})
    asyncio.create_task(inbound_listener())   # 완전 실시간 인박스 소비(폴링과 병행)
    asyncio.create_task(job_heartbeat())      # 기동 즉시 하트비트 1회(딥 헬스 60s 대기 회피)
    asyncio.create_task(_watch_scheduler_lock(lock_conn))   # 락 상실 시 재시작(이중 리더 방지)
    logger.info("worker-batch 기동", extra={"ctx": {"jobs": [j.id for j in sched.get_jobs()]}})
    await asyncio.Event().wait()  # 상주


if __name__ == "__main__":
    asyncio.run(main())
