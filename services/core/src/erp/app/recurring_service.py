"""정기업무 러너(erp.app 오케스트레이션) — due 물질화·claim-then-run·핸들러 실행·실행이력.

common.recurring(순수 커널)의 주기엔진·핸들러 레지스트리를 사용한다. common은 도메인을 import할 수
없으므로 실행 계층은 여기(app)에 둔다. 메타잡(worker-batch)이 run_due()를 주기 호출한다.

멱등/안전:
- **claim-then-run**(R1): ``UPDATE ... WHERE status='DUE'`` 로 DUE→RUNNING 선점, rowcount==1인
  워커만 핸들러 실행 → 병행 발화(APScheduler 섀도우 이관기)에도 이중실행 없음.
- **due 물질화**(R3): enabled 업무마다 현재 기간 DUE 행 보장 → OVERDUE·화면·알림·AI due 성립.
- 핸들러는 savepoint 격리(BI 마트 패턴) — 개별 실패가 이력 기록/다른 잡을 오염시키지 않음.
- error=str(exc)[:120](Phase0 불변식). 자동실행 대상=AUTO·enabled·!requires_approval만.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common import recurring as rec
from erp.common.kst import as_kst_wallclock
from erp.common.models import RecurringTask, RecurringTaskRun
from erp.errors import ValidationError

logger = logging.getLogger(__name__)
SYSTEM_ACTOR = 0   # 스케줄러/CATCHUP 실행 주체(기존 integrations SYSTEM_ACTOR 관례 계승)


def _registered_after_due(task, sched: datetime) -> bool:
    """사람이 하는(MANUAL) 업무를 그 기간 예정 시각이 이미 지난 뒤 등록했는가 — 그 기간 일은 그때 다른 방식으로 처리됐다.

    2026-10 연간 업무 신설 시 2026-02·03 신고가 '지연'으로 만들어져 매 틱 알림·주간 브리핑 지연 목록이 되던 결함(연말 대비
    검토 MED). 자동 실행 업무는 등록 직후라도 그 기간을 실행한다(시스템이 하는 일). run_due·due_list 공용 단일 규칙.
    """
    created = as_kst_wallclock(task.created_at)
    return created is not None and task.execution_mode == "MANUAL" and sched < created


class RecurringRunner:
    async def _get_run(self, session: AsyncSession, code: str, pkey: str) -> RecurringTaskRun | None:
        return await session.scalar(select(RecurringTaskRun).where(
            RecurringTaskRun.task_code == code, RecurringTaskRun.period_key == pkey))

    async def _ensure_run(self, session: AsyncSession, *, code: str, pkey: str,
                          sched: datetime) -> RecurringTaskRun:
        """(code, period_key) DUE 행 보장 — 경쟁 삽입은 UNIQUE로 흡수 후 재조회(savepoint 격리)."""
        run = await self._get_run(session, code, pkey)
        if run is not None:
            return run
        run = RecurringTaskRun(task_code=code, period_key=pkey, scheduled_for=sched, status="DUE")
        try:
            async with session.begin_nested():
                session.add(run)
                await session.flush()
            return run
        except IntegrityError:
            return await self._get_run(session, code, pkey)      # 타 워커가 이미 생성

    async def _claim(self, session: AsyncSession, code: str, pkey: str) -> bool:
        """DUE→RUNNING 원자 선점 — rowcount==1인 워커만 실행 진행."""
        res = await session.execute(
            update(RecurringTaskRun)
            .where(RecurringTaskRun.task_code == code, RecurringTaskRun.period_key == pkey,
                   RecurringTaskRun.status == "DUE")
            .values(status="RUNNING", started_at=rec.now_naive()))
        return (res.rowcount or 0) == 1

    async def _execute(self, session: AsyncSession, task: RecurringTask,
                       run: RecurringTaskRun) -> list[tuple[str, str, str]]:
        """등록 핸들러 실행(savepoint 격리) → DONE/FAILED 기록. run은 claim 후 RUNNING 상태.

        반환=핸들러가 ctx["post_commit_emails"]에 스테이징한 (수신, 제목, 본문) — 호출자가 커밋
        **후** _send_emails로 발송(롤백 시 미발송 보장·주소는 run.result에 미저장). FAILED면 빈 목록.
        """
        # ⚠ started는 Python 시점 캡처 — DB(timestamptz) 라운드트립 TZ 왜곡(±offset) 회피(실측 소요시간).
        started = rec.now_naive()
        handler = rec.get_handler(task.handler_ref)
        ctx: dict = {"code": task.code, "post_commit_emails": []}
        try:
            if handler is None:
                raise RuntimeError(f"핸들러 미등록: {task.handler_ref}")
            async with session.begin_nested():
                result = await handler(session, run.period_key, ctx)
            run.status = "DONE"
            run.result = result if isinstance(result, dict) else {"ok": True}
            run.error = None
        except Exception as exc:  # noqa: BLE001 — 개별 잡 실패 격리(다른 잡·이력 기록 보호)
            run.status = "FAILED"
            run.error = str(exc)[:120]
            logger.warning("정기업무 실패", extra={"ctx": {"code": task.code, "err": str(exc)[:120]}})
        finally:
            run.finished_at = rec.now_naive()
            run.duration_ms = int((run.finished_at - started).total_seconds() * 1000)
        return ctx["post_commit_emails"] if run.status == "DONE" else []

    async def _send_emails(self, emails: list[tuple[str, str, str]]) -> int:
        """커밋 후 이메일 발송 — send_email은 무예외(False)·SMTP 미설정 no-op. 반환=성공 수."""
        if not emails:
            return 0
        import asyncio

        from erp.common.email import send_email
        sent = 0
        for to, subject, body in emails:
            if await asyncio.to_thread(send_email, to, subject, body):
                sent += 1
        return sent

    async def _notify_owner(self, session: AsyncSession, task: RecurringTask, *, ntype: str,
                            title: str, body: str, href: str) -> int:
        """담당 역할(owner_role)에 인앱 알림 — body 디둡(반복 소음 방지). 반환=발송 수.

        ⚠ **담당 역할에 사람이 0명이면 조용히 유실됐다.** prod 실측(2026-07-30): 활성 업무의
        `FINANCE_MANAGER`(AI 1호 주간 미수금)·`LOGISTICS_MANAGER`(월 재고 실사) 담당 역할은
        보유자가 **0명**이라, 초안 준비 알림과 SLA 리마인드가 아무에게도 가지 않으면서
        발송 0건을 성공으로 돌려줬다 — "열어보지 않아도 닿게 한다"의 정반대다.

        0명이면 **ADMIN으로 폴백**하고 **본문에 원인(역할 미배정)을 적는다**. 알림을 잃지 않으면서
        받는 사람이 조치(역할 배정 또는 담당 변경)를 알 수 있어야 하기 때문이다 — 폴백만 하고
        원인을 숨기면 관리자는 남의 알림을 계속 받고 미배정은 영구히 남는다.
        """
        if not task.owner_role:
            return 0
        from erp.common.models import Notification, User
        # 활성 계정만 — 비활성(퇴사 차단 등) 계정만 남은 역할은 미배정과 같다(알림이 읽히지 않고 폴백도 안 됐다·2026-10-11)
        ids = (await session.execute(
            select(User.id).where(User.role == task.owner_role, User.is_active.is_(True)))).scalars().all()
        if not ids:
            ids = (await session.execute(
                select(User.id).where(User.role == "ADMIN", User.is_active.is_(True)))).scalars().all()
            body = f"{body}\n(담당 역할 {task.owner_role} 미배정 — 관리자에게 통지)"
        added = 0
        for uid in ids:
            dup = await session.scalar(select(Notification.id).where(
                Notification.user_id == uid, Notification.type == ntype,
                Notification.read_at.is_(None), Notification.body == body).limit(1))
            if dup is None:
                session.add(Notification(user_id=uid, type=ntype, title=title,
                                         body=body, href=href))
                added += 1
        return added

    async def _remind_owner(self, session: AsyncSession, task: RecurringTask,
                            run: RecurringTaskRun) -> int:
        """지연된 MANUAL/AI_ASSISTED 업무의 담당 역할에 리마인드 — 발송 수.

        ⚠ **문구는 모드별로 다르다**. 이전에는 전부 "완료 처리 필요"·`/admin/recurring-tasks`였는데,
        AI_ASSISTED는 `complete_manual`이 거부하는 모드다("자동/AI 업무는 완료처리 대신 실행하세요")
        — 알림이 **시스템이 거부하는 조치를 지시**하고 있었다. 받는 사람이 할 수 있는 행위와 그
        장소를 가리켜야 한다(초안 대기 → 결정 대기함).
        """
        if task.execution_mode == "AI_ASSISTED":
            if run.status == "WAITING_APPROVAL":
                body = f"[{run.period_key}] '{task.name}' AI 초안 승인 대기 — 검토·승인 필요"
            else:
                body = f"[{run.period_key}] '{task.name}' AI 초안 준비 필요"
            href = "/admin/decisions"
        else:
            body = f"[{run.period_key}] '{task.name}' 처리 기한 경과 — 완료 처리 필요"
            href = "/admin/recurring-tasks"
        return await self._notify_owner(session, task, ntype="RECURRING_TASK_DUE",
                                       title="정기업무 지연", body=body, href=href)

    async def run_due(self, session: AsyncSession, *, now: datetime | None = None,
                      modes: tuple[str, ...] = ("AUTO",), dry_run: bool = False, catchup: int = 1,
                      actor: int = SYSTEM_ACTOR, triggered_by: str = "SCHEDULER",
                      auto_prepare: bool = True) -> dict:
        """예정 시각 도달·미완료 업무를 물질화하고, 자동실행 대상만 claim 후 실행.

        auto_prepare(기본 True): AI_ASSISTED 업무의 **초안까지** 만들어 결정 대기함에 올린다.
        `modes`(자동 **실행** 대상)와 별개 스위치인 이유는 초안이 실행이 아니기 때문이다 —
        부작용 없이 읽어서 승인 요청만 만들고, 확정은 사람 승인 후 `sync_ai_decisions`가 한다.
        dry_run=True(섀도우 이관기): 물질화만 하고 실행하지 않는다(APScheduler 병행 검증용).
        catchup(N>0, M/Q/Y만): 직전 N개 기간까지 소급 처리 — 워커가 기간을 통째로 놓친 경우 복구.
        **기본 1** — PERIOD_END 앵커(월말+3일·분기말+25일)는 현재 기간 예정 시각이 늘 미래라
        catchup=0이면 물질화조차 안 된다. 관리화면/CLI의 수동 일괄 실행이 그 업무들에 대해
        조용한 no-op이 되던 결함(적대검토 A-H2) — 스케줄러와 동일 기본값으로 통일한다.
        **created_at 게이팅**: 업무 생성 이전에 이미 종료된 기간은 백필하지 않는다(허위 소급 방지·F3).
        단일 트랜잭션 실행 — 무거운 잡 이관 시 메타잡이 업무별 커밋으로 감싸는 것을 권장.
        """
        now = now or rec.now_naive()
        today = now.date()
        resolved = await self.sync_ai_decisions(session)   # 확정된 AI_ASSISTED 결정부터 반영
        tasks = (await session.scalars(
            select(RecurringTask).where(RecurringTask.enabled.is_(True)))).all()
        summary = {"materialized": 0, "executed": 0, "done": 0, "failed": 0, "reminded": 0,
                   "emailed": 0, "prepared": 0, "notified": 0,
                   "ai_resolved": resolved["done"] + resolved["skipped"]}
        for task in tasks:
            created = as_kst_wallclock(task.created_at)
            created_date = created.date() if created is not None else date.min
            dates = [today]
            if catchup > 0 and task.cadence in ("MONTHLY", "QUARTERLY", "YEARLY"):
                d = today
                for _ in range(catchup):
                    d = rec.shift_back(task.cadence, d)
                    dates.append(d)
            for pd in sorted(dates):                  # 과거→현재 순으로 소급
                if rec.period_bounds(task.cadence, pd)[1] < created_date:
                    continue                          # 업무 생성 전 종료된 기간 — 백필 안 함
                sched = rec.scheduled_for(task.cadence, pd, task.anchor)
                if now < sched:
                    continue                          # 아직 예정 시각 전
                if _registered_after_due(task, sched):
                    continue
                pkey = rec.period_key(task.cadence, pd)
                run = await self._ensure_run(session, code=task.code, pkey=pkey, sched=sched)
                if run is None:
                    continue
                overdue = now > sched + timedelta(days=task.sla_days)
                if run.status == "WAITING_APPROVAL":
                    continue        # 승인 대기 리마인드는 루프 밖 전용 패스(_remind_pending_approvals)
                ai_draft = (task.execution_mode == "AI_ASSISTED" and auto_prepare and not dry_run
                            and rec.get_handler(task.handler_ref) is not None)
                # 초안 실패는 **다음 틱에 재시도**한다(읽기 전용이라 재시도가 안전). 그냥 두면 DUE도
                # WAITING_APPROVAL도 아니어서 리마인드조차 닿지 않는 조용한 구멍이 된다 — 자동
                # 준비가 켜지면 이 실패는 아무도 안 보는 시각에 일어난다.
                # 주·월·분기 AUTO 실패는 1시간 뒤 최대 3회까지 재시도한다 — DAILY는 상태 기반이라 다음 날
                # 자가치유되지만, 월간 잡은 PG 순단 한 번이면 그 달이 영구 미실행으로 남았다(2026-10-09 검토 F).
                retry_attempt = 0
                if (run.status == "FAILED" and not dry_run and task.execution_mode == "AUTO"
                        and task.cadence != "DAILY" and not task.requires_approval):
                    attempts = int((run.result or {}).get("_attempts", 1))
                    fin = as_kst_wallclock(run.finished_at)
                    if attempts < 3 and fin is not None and now - fin >= timedelta(hours=1):
                        # 조건부 UPDATE — 수동 실행 등 다른 워커가 그 사이 DONE으로 바꿨으면 덮지 않는다(2차 검토 F)
                        res = await session.execute(
                            update(RecurringTaskRun).where(
                                RecurringTaskRun.id == run.id,
                                RecurringTaskRun.status == "FAILED",
                                RecurringTaskRun.finished_at == run.finished_at)
                            .values(status="DUE"))
                        if (res.rowcount or 0) == 1:
                            await session.refresh(run)
                            retry_attempt = attempts + 1
                if run.status != "DUE" and not (ai_draft and run.status == "FAILED"):
                    continue                          # 이미 실행중/완료/생략
                if run.status == "DUE":
                    summary["materialized"] += 1
                    await session.commit()            # 물질화 durable(잡별 독립 커밋·F2)
                if ai_draft:
                    # AI 보조 초안은 **부작용이 없다**(읽기만·PendingDecision 큐잉) — 사람이 화면에서
                    # "초안 준비"를 누를 때까지 기다릴 이유가 없었다. 종전 구조에서는 아무도 누르지
                    # 않으면 초안이 영영 생기지 않아, 담당자는 "지연" 알림만 받고 정작 검토할 초안은
                    # 없었다(prod 실측: 첫 주 run이 DUE로 방치). requires_approval과 무관하게 안전한
                    # 이유도 같다 — 초안 생성 자체가 승인 요청이고, 실행은 승인 후 sync가 한다.
                    await self._stage_draft(session, task, run, actor=actor,
                                            triggered_by=triggered_by)
                    if run.status == "WAITING_APPROVAL":
                        summary["prepared"] += 1
                        if await self._notify_owner(
                                session, task, ntype="RECURRING_AI_DRAFT",
                                title="AI 초안 준비 완료",
                                body=(f"[{pkey}] '{task.name}' 초안이 준비됐습니다 — "
                                      "결정·조치함에서 검토·승인하세요"),
                                href="/admin/decisions"):
                            await session.commit()
                            summary["notified"] += 1
                    else:
                        summary["failed"] += 1        # 초안 생성 실패(FAILED 기록·격리)
                    continue
                if task.execution_mode not in modes or task.requires_approval:
                    # 수기·승인필요 → DUE로 남김. 기한 경과 시 담당 역할에 리마인드(C).
                    if not dry_run and overdue:
                        if await self._remind_owner(session, task, run):
                            await session.commit()
                            summary["reminded"] += 1
                    continue
                if dry_run:
                    continue                          # 섀도우 — 물질화만
                if not await self._claim(session, task.code, pkey):
                    continue                          # 타 워커 선점
                await session.refresh(run)            # claim 후 RUNNING 반영
                run.triggered_by, run.actor = triggered_by, actor
                emails = await self._execute(session, task, run)
                if retry_attempt:
                    run.result = {**(run.result or {}), "_attempts": retry_attempt}
                if (run.status == "FAILED" and task.cadence != "DAILY"
                        and (retry_attempt or 1) >= 3):
                    # 재시도 소진 — 담당에게 알린다(종전엔 실패 알림이 없어 그 달이 조용히 비었다)
                    await self._notify_owner(
                        session, task, ntype="RECURRING_FAILED", title="정기업무 자동 실행 실패",
                        body=f"[{pkey}] '{task.name}' 자동 실행이 3회 실패했습니다 — 정기업무 화면에서 "
                             "사유 확인 후 수동 실행하세요", href="/admin/recurring-tasks")
                await session.commit()                # 잡별 커밋 — 락 조기해제·개별 durability(F2)
                summary["emailed"] += await self._send_emails(emails)   # 커밋 후 발송
                summary["executed"] += 1
                summary["done" if run.status == "DONE" else "failed"] += 1
        if not dry_run:
            summary["reminded"] += await self._remind_pending_approvals(session, now=now)
        return summary

    async def _remind_pending_approvals(self, session: AsyncSession, *, now: datetime) -> int:
        """방치된 승인 대기(WAITING_APPROVAL) 리마인드 — **기간 루프와 독립**. 발송 수.

        루프 안에 두면 닿지 않는 대기 건이 생긴다. 루프는 `[오늘, catchup 직전 기간]`만 방문하고
        **created_at 게이팅**(업무 생성 전 종료된 기간은 백필 안 함·F3)으로 더 걸러지는데,
        `prepare_ai_assisted`(수동)는 그 게이팅 없이 `due_period`로 초안을 만들 수 있다.
        prod 실측(2026-07-30): 수동 준비된 `tax.vat_quarterly` **2026-Q2** 초안이 기한을 5일
        넘겼는데도, 그 기간(종료 6/30 < 업무생성 7/27)을 루프가 방문하지 않아 알림이 없었다.
        catchup 창을 벗어난 과거 기간도 같은 사각지대다.

        승인되면 `sync_ai_decisions`가 DONE으로 바꿔 대상에서 빠진다 — 스스로 멈추는 조건부
        알림이다(영구 발화 금지). 시각 비교는 timestamptz 되읽기 왜곡 회피로 Python에서 한다.
        """
        rows = (await session.execute(
            select(RecurringTaskRun, RecurringTask)
            .join(RecurringTask, RecurringTask.code == RecurringTaskRun.task_code)
            .where(RecurringTaskRun.status == "WAITING_APPROVAL",
                   RecurringTask.enabled.is_(True)))).all()
        sent = 0
        for run, task in rows:
            sched = as_kst_wallclock(run.scheduled_for)
            if sched is None or now <= sched + timedelta(days=task.sla_days):
                continue
            if await self._remind_owner(session, task, run):
                await session.commit()
                sent += 1
        return sent

    async def reclaim_orphan_runs(self, session: AsyncSession) -> dict:
        """RUNNING인 채 버려진 run을 DUE로 회수 — 스케줄러 리더가 **기동 직후 1회** 호출.

        워커가 핸들러 실행 중 죽으면(배포 kill·OOM·호스트 재부팅) claim 시 커밋된 RUNNING 행이
        영구 잔류한다. run_due는 `status != "DUE"`를 건너뛰므로 그 기간은 다시 실행되지 않고
        (DAILY는 다음날 새 기간으로 넘어가지만 MONTHLY/QUARTERLY는 **기간 전체가 소실**),
        stuck_running 신호가 영구 발화해 deep-health가 상시 503이 된다(적대검토 B-F1).

        **왜 안전한가**: 스케줄러는 advisory lock 리더 1인만 잡을 발화하고(max_instances=1),
        이 함수는 락을 **새로 획득한 직후**에만 돈다. 새 리더가 섰다는 것은 이전 락 보유 커넥션이
        사라졌다는 뜻이고, 그러면 그 프로세스의 열린 트랜잭션도 함께 파기된 상태다 — 즉 남아 있는
        RUNNING은 정의상 고아이고, 그 잡의 부분 쓰기는 이미 롤백돼 재실행이 안전하다.
        """
        runs = (await session.scalars(select(RecurringTaskRun).where(
            RecurringTaskRun.status == "RUNNING"))).all()
        for run in runs:
            run.status = "DUE"
            run.started_at = None
            run.result = {**(run.result or {}), "reclaimed": True,
                          "reclaimed_reason": "worker restart — orphan RUNNING"}
        if runs:
            await session.commit()
            logger.warning("고아 RUNNING 회수", extra={"ctx": {
                "count": len(runs), "codes": sorted({r.task_code for r in runs})}})
        return {"reclaimed": len(runs)}

    async def health_signal(self, session: AsyncSession, *, now: datetime | None = None) -> dict:
        """자동실행 경로 이상 신호(R11) — deep-health·관리화면 공용.

        - failed_recent: 최근 48h 내 **발생한** FAILED run. 같은 업무가 그 뒤 성공했으면 해제.
        - stuck_due: AUTO·enabled·비승인 업무의 **최신** DUE가 예정+2h 경과(메타잡 매시:20 — 2틱 초과).
        - stuck_running: RUNNING 시작 2h 경과(크래시 고아 — claim 후 미완료).
        - engine_stale: enabled AUTO 존재하는데 최근 26h 실행 이력 전무(DAILY 잡 상시 → 메타잡 사망).
        MANUAL/AI_ASSISTED 지연은 사람 업무(_remind_owner가 처리)라 제외. 시각 비교는 timestamptz
        라운드트립 왜곡 회피 위해 Python(as_kst_wallclock·UTC aware→KST 환산)에서 수행.

        **영구 발화 방지**(적대검토 B-F1~F3): 이 신호는 deep-health 503의 원천이므로, 한 번 켜지면
        스스로 꺼지지 않는 조건이 있으면 모니터 자체가 무가치해진다.
        - F3: 실패 판정 기준을 `scheduled_for`→`finished_at`으로. 전자는 catchup 소급 실행의 실패를
          "48h 이전 예정"으로 보고 **처음부터 놓쳤고**, 복구 성공 후에도 과거 FAILED 행이 남아
          이틀간 적색 고정이었다. 이후 성공 이력이 있으면 해제한다.
        - F2: 과거 기간의 DUE 행 전부가 아니라 **업무별 최신 1건**만 본다. MANUAL→AUTO 전환처럼
          미완료 DUE가 N일치 쌓인 정상 조작이 즉시 N건 발화를 일으키던 경로를 차단.
        - F1: 고아 RUNNING은 스케줄러 리더가 기동 시 회수한다(reclaim_orphan_runs).
        """
        now = now or rec.now_naive()
        rows = (await session.execute(
            select(RecurringTaskRun, RecurringTask)
            .join(RecurringTask, RecurringTask.code == RecurringTaskRun.task_code)
            .where(RecurringTaskRun.status.in_(("FAILED", "DUE", "RUNNING"))))).all()
        # 업무별 마지막 성공 시각 — 이후 성공했다면 과거 실패는 해제(F3)
        last_ok: dict[str, datetime] = {}
        for code, fin in (await session.execute(
                select(RecurringTaskRun.task_code, func.max(RecurringTaskRun.finished_at))
                .where(RecurringTaskRun.status == "DONE")
                .group_by(RecurringTaskRun.task_code))).all():
            ok_at = as_kst_wallclock(fin)
            if ok_at is not None:
                last_ok[code] = ok_at
        failed, stuck_running = set(), set()
        latest_due: dict[str, datetime] = {}          # 업무별 최신 DUE 예정 시각(F2)
        for run, task in rows:
            sched = as_kst_wallclock(run.scheduled_for)
            if run.status == "FAILED":
                at = as_kst_wallclock(run.finished_at) or sched
                if at is None or now - at > timedelta(hours=48):
                    continue                          # 오래된 실패 — 자동 해제
                if last_ok.get(run.task_code, datetime.min) > at:
                    continue                          # 그 뒤 성공 — 복구됨
                failed.add(run.task_code)
            elif run.status == "DUE":
                if (task.enabled and task.execution_mode == "AUTO"
                        and not task.requires_approval and sched is not None):
                    prev = latest_due.get(run.task_code)
                    if prev is None or sched > prev:
                        latest_due[run.task_code] = sched
            elif run.status == "RUNNING":
                started = as_kst_wallclock(run.started_at)
                if started is not None and now - started > timedelta(hours=2):
                    stuck_running.add(run.task_code)
        stuck_due = {code for code, sched in latest_due.items()
                     if now - sched > timedelta(hours=2)}
        engine_stale = False
        has_auto = await session.scalar(select(RecurringTask.id).where(
            RecurringTask.enabled.is_(True), RecurringTask.execution_mode == "AUTO",
            RecurringTask.requires_approval.is_(False)).limit(1))
        if has_auto is not None:
            last = await session.scalar(
                select(RecurringTaskRun.finished_at)
                .where(RecurringTaskRun.finished_at.is_not(None))
                .order_by(RecurringTaskRun.finished_at.desc()).limit(1))
            if last is None:   # 이력 전무(신규 시드 직후) — 정의 생성 시점 기준으로 유예
                last = await session.scalar(select(func.min(RecurringTask.created_at)).where(
                    RecurringTask.enabled.is_(True), RecurringTask.execution_mode == "AUTO"))
            last = as_kst_wallclock(last)
            engine_stale = last is None or (now - last) > timedelta(hours=26)
        return {"ok": not (failed or stuck_due or stuck_running or engine_stale),
                "failed_recent": sorted(failed), "stuck_due": sorted(stuck_due),
                "stuck_running": sorted(stuck_running), "engine_stale": engine_stale}

    async def weekly_briefing(self, session: AsyncSession, *, now: datetime | None = None) -> dict:
        """주간 운영 브리핑(M4 백로그) — 최근 7일 실행 요약·현재 due/지연·이상 신호. PII 없는 집계만.

        핸들러(common.weekly_briefing·월요일 아침 알림)와 AI CLI(brief)가 공용. 시각 비교는
        Python(as_kst_wallclock·UTC aware→KST 환산) — timestamptz 라운드트립 왜곡 회피. 최근 500건만 스캔(7일 창 커버).
        """
        now = now or rec.now_naive()
        since = now - timedelta(days=7)
        rows = (await session.scalars(
            select(RecurringTaskRun).where(RecurringTaskRun.finished_at.is_not(None))
            .order_by(RecurringTaskRun.finished_at.desc()).limit(500))).all()
        done = 0
        failed: list[dict] = []
        for r in rows:
            fin = as_kst_wallclock(r.finished_at)
            if fin is None or fin < since:
                continue
            if r.status == "DONE":
                done += 1
            elif r.status == "FAILED":
                failed.append({"code": r.task_code, "period_key": r.period_key,
                               "error": r.error})
        due_rows = await self.due_list(session, now=now)
        overdue = [{"code": d["code"], "period_key": d["period_key"],
                    "mode": d["execution_mode"], "owner_role": d["owner_role"]}
                   for d in due_rows if d["overdue"]]
        return {"window": {"from": since.date().isoformat(), "to": now.date().isoformat()},
                "runs_7d": {"done": done, "failed": len(failed)},
                "failed_7d": failed[:10],
                "overdue": overdue,
                "due_now": [d["code"] for d in due_rows if d["status"] == "DUE"],
                "signal": await self.health_signal(session, now=now)}

    async def run_one(self, session: AsyncSession, *, code: str, actor: int,
                      triggered_by: str = "ADMIN_UI", force: bool = False) -> RecurringTaskRun:
        """단건 수동 실행(관리화면·AI CLI) — DUE/FAILED 또는 force 시 재실행. AUTO/AI_ASSISTED만."""
        task = await session.scalar(select(RecurringTask).where(RecurringTask.code == code))
        if task is None:
            raise ValidationError(f"존재하지 않는 정기업무: {code}")
        if task.requires_approval:
            raise ValidationError("승인 필요 업무는 자동/수동 즉시 실행할 수 없습니다")
        if rec.get_handler(task.handler_ref) is None:
            raise ValidationError(f"실행 가능한 핸들러가 없습니다: {code}")
        pd = rec.due_period(task.cadence, task.anchor, rec.now_naive())
        if pd is None:      # PERIOD_END 앵커 등 — 아직 어떤 기간도 예정 시각 미도달
            raise ValidationError("아직 실행 예정 시각에 도달한 기간이 없습니다")
        pkey = rec.period_key(task.cadence, pd)
        sched = rec.scheduled_for(task.cadence, pd, task.anchor)
        run = await self._ensure_run(session, code=code, pkey=pkey, sched=sched)
        if run is None:                               # 경쟁 삽입 후 재조회 실패(이례적)
            raise ValidationError("실행 레코드 생성 실패 — 재시도하세요")
        if run.status in ("RUNNING", "DONE") and not force:
            return run                                # 이미 처리(재실행은 force)
        if force:                                     # FAILED/DONE 재실행 — 상태 재개방
            run.status = "DUE"
            await session.flush()
        if not await self._claim(session, code, pkey):
            return await self._get_run(session, code, pkey)
        await session.refresh(run)
        run.triggered_by, run.actor = triggered_by, actor
        emails = await self._execute(session, task, run)
        await session.commit()
        await self._send_emails(emails)               # 커밋 후 발송(post-commit 채널)
        return run

    async def complete_manual(self, session: AsyncSession, *, code: str, actor: int,
                              note: str | None = None) -> RecurringTaskRun:
        """수기(MANUAL) 업무 완료 처리 — 현재 기간 run을 DONE으로. AUTO/AI_ASSISTED는 run_one 사용."""
        task = await session.scalar(select(RecurringTask).where(RecurringTask.code == code))
        if task is None:
            raise ValidationError(f"존재하지 않는 정기업무: {code}")
        if task.execution_mode != "MANUAL":
            raise ValidationError("자동/AI 업무는 완료처리 대신 실행하세요")
        pd = rec.due_period(task.cadence, task.anchor, rec.now_naive())
        if pd is None:
            raise ValidationError("아직 실행 예정 시각에 도달한 기간이 없습니다")
        pkey = rec.period_key(task.cadence, pd)
        sched = rec.scheduled_for(task.cadence, pd, task.anchor)
        run = await self._ensure_run(session, code=code, pkey=pkey, sched=sched)
        if run.status == "DONE":
            return run
        run.status = "DONE"
        run.actor, run.triggered_by = actor, "ADMIN_UI"
        run.finished_at = rec.now_naive()
        run.result = {"note": (note or "")[:200], "manual": True}
        await session.commit()
        return run

    # ── AI_ASSISTED: 초안 준비 → 결정 대기함 스테이징 → 결정 반영(R6) ──────────────
    _APPROVE_OPTS = [{"key": "approve", "label": "승인", "recommended": True},
                     {"key": "reject", "label": "반려"}]
    # AI 보조 도입 업무(결정 `rectask-ai-assisted-scope` 권고) — code → (mode, handler_ref).
    # 1호=주간 미수금 점검, 2호=부가세 초안(집계까지만·신고는 세무사/사람). HR 법정신고
    # (4대보험·원천세·연말정산)는 **의도적 제외** — 금액은 이미 결정론적 계산 엔진이 산출하고,
    # AI가 끼면 책임 소재만 흐려진다.
    _AI_ASSISTED = {"ops.ar_review_weekly": ("AI_ASSISTED", "ops.ar_review_weekly"),
                    "tax.vat_quarterly": ("AI_ASSISTED", "tax.vat_quarterly")}

    async def prepare_ai_assisted(self, session: AsyncSession, *, code: str,
                                  actor: int) -> RecurringTaskRun:
        """AI_ASSISTED 초안 준비 — 핸들러가 만든 제안을 PendingDecision(결정 대기함)으로 큐잉하고
        run을 WAITING_APPROVAL로. 사람이 /admin/decisions에서 승인/반려 → sync_ai_decisions가 확정.

        핸들러 반환(dict)에서 question/options/context_ref/summary를 읽어 결정 항목을 구성한다.
        (핸들러는 부작용 없는 '초안 생성'만 — 실제 반영은 승인 후.)
        """
        task = await session.scalar(select(RecurringTask).where(RecurringTask.code == code))
        if task is None:
            raise ValidationError(f"존재하지 않는 정기업무: {code}")
        if task.execution_mode != "AI_ASSISTED":
            raise ValidationError("AI_ASSISTED 업무만 초안 준비 대상입니다")
        if rec.get_handler(task.handler_ref) is None:
            raise ValidationError(f"초안 핸들러가 없습니다: {code}")
        pd = rec.due_period(task.cadence, task.anchor, rec.now_naive())
        if pd is None:      # PERIOD_END 앵커 등 — 아직 어떤 기간도 예정 시각 미도달(A-H1)
            raise ValidationError("아직 실행 예정 시각에 도달한 기간이 없습니다")
        pkey = rec.period_key(task.cadence, pd)
        sched = rec.scheduled_for(task.cadence, pd, task.anchor)
        run = await self._ensure_run(session, code=code, pkey=pkey, sched=sched)
        if run is None:
            raise ValidationError("실행 레코드 생성 실패 — 재시도하세요")
        if run.status in ("WAITING_APPROVAL", "DONE"):
            return run                                    # 이미 준비/확정(멱등)
        return await self._stage_draft(session, task, run, actor=actor, triggered_by="AI_CLI")

    async def _stage_draft(self, session: AsyncSession, task: RecurringTask,
                           run: RecurringTaskRun, *, actor: int,
                           triggered_by: str) -> RecurringTaskRun:
        """초안 생성 → 결정 대기함 큐잉 → run WAITING_APPROVAL(공용 경로).

        **이미 물질화된 run을 받는다** — 기간 선정은 호출자 책임이다. 수동 경로
        (`prepare_ai_assisted`)는 `due_period`로, 스케줄러 경로(`run_due`)는 루프가 도는 기간으로
        고르는데, 이 함수가 기간을 다시 고르면 catchup 중 **호출자가 잡은 기간과 어긋난 기간을
        건드린다**(그 기간 run을 선점해 정규 실행을 막는 A-H1의 변형).
        """
        from erp.common.models import PendingDecision
        pkey = run.period_key
        if run.status == "FAILED":
            # 실패한 초안은 **다시 준비할 수 있어야 한다**. `_claim`은 DUE에서만 전이하므로 재개방이
            # 없으면 화면의 "초안 준비"가 같은 FAILED run을 돌려주는 **조용한 no-op**이 된다
            # (화면은 성공 토스트를 띄우고 아무 일도 일어나지 않았다). 초안은 부작용이 없어 안전하다.
            run.status, run.error = "DUE", None
            await session.flush()
        if not await self._claim(session, task.code, pkey):   # DUE→RUNNING 선점
            return await self._get_run(session, task.code, pkey)
        await session.refresh(run)
        # 초안 생성(savepoint 격리)
        handler = rec.get_handler(task.handler_ref)
        try:
            async with session.begin_nested():
                proposal = await handler(session, pkey, {"code": task.code, "mode": "prepare"})
        except Exception as exc:  # noqa: BLE001 — 초안 실패는 FAILED로 기록(격리)
            run.status, run.error = "FAILED", str(exc)[:120]
            run.finished_at, run.actor = rec.now_naive(), actor
            await session.commit()      # RUNNING 잔류 방지(스케줄러 경로는 커밋 주체가 없다)
            return run
        proposal = proposal if isinstance(proposal, dict) else {}
        dkey = f"rectask:{task.code}:{pkey}"
        options = proposal.get("options") or self._APPROVE_OPTS
        existing = await session.scalar(select(PendingDecision).where(PendingDecision.key == dkey))
        if existing is None:
            session.add(PendingDecision(
                key=dkey, category="OPS", title=f"{task.name} ({pkey})",
                question=(proposal.get("question") or f"'{task.name}' {pkey} 초안을 검토·승인하세요."),
                options=options, context_ref=proposal.get("context_ref"), created_by=actor))
            await session.flush()
        run.status, run.decision_key = "WAITING_APPROVAL", dkey
        run.triggered_by, run.actor = triggered_by, actor
        run.result = {k: v for k, v in proposal.items() if k in ("summary", "counts")}
        run.finished_at = None
        await session.commit()
        return run

    async def sync_ai_decisions(self, session: AsyncSession) -> dict:
        """WAITING_APPROVAL run들의 연결 PendingDecision 결정을 반영 — 승인=DONE·반려=SKIPPED."""
        from erp.common.models import PendingDecision
        runs = (await session.scalars(select(RecurringTaskRun).where(
            RecurringTaskRun.status == "WAITING_APPROVAL",
            RecurringTaskRun.decision_key.is_not(None)))).all()
        out = {"done": 0, "skipped": 0}
        for run in runs:
            d = await session.scalar(select(PendingDecision).where(
                PendingDecision.key == run.decision_key))
            if d is None or d.status != "DECIDED":
                continue                                  # 아직 미결정
            approved = d.decided_option in ("approve", "apply", "yes")
            run.status = "DONE" if approved else "SKIPPED"
            run.finished_at = rec.now_naive()
            run.actor = d.decided_by
            out["done" if approved else "skipped"] += 1
        await session.commit()
        return out

    async def compliance_context(self, session: AsyncSession, *, as_of=None,
                                 horizon_days: int = 90, limit: int = 20) -> list[dict]:
        """관련 법정기한 링크아웃(R4) — ComplianceDeadlineService 재사용(복제 금지). 읽기전용 컨텍스트."""
        from erp.app.compliance_service import ComplianceDeadlineService
        from erp.common.kst import kst_today
        data = await ComplianceDeadlineService().deadlines(
            session, as_of=as_of or kst_today(), horizon_days=max(1, min(horizon_days, 365)))
        items = data.get("items", data) if isinstance(data, dict) else data
        return items[:limit] if isinstance(items, list) else []

    async def list_tasks(self, session: AsyncSession) -> list[dict]:
        """정의 목록 — `owner_recipients`(담당 역할 보유자 수)를 함께 준다.

        ⚠ 0명이면 알림·리마인드가 **관리자 폴백으로만** 전달된다(`_notify_owner`). prod 실측
        (2026-07-30)에서 `FINANCE_MANAGER`·`LOGISTICS_MANAGER`가 0명이었고, 그 사실이 어디에도
        보이지 않아 알림이 유실되는 줄 몰랐다 — 화면이 보여주고 그 자리에서 담당을 바꿀 수 있어야
        운영이 스스로 해소할 수 있다(역할 배정은 `/admin/users`).
        """
        from erp.common.models import User
        rows = (await session.scalars(
            select(RecurringTask).order_by(RecurringTask.cadence, RecurringTask.code))).all()
        counts = dict((await session.execute(
            select(User.role, func.count(User.id)).where(User.is_active.is_(True))
            .group_by(User.role))).all())
        return [{"code": t.code, "name": t.name, "domain": t.domain, "category": t.category,
                 "cadence": t.cadence, "anchor": t.anchor, "execution_mode": t.execution_mode,
                 "handler_ref": t.handler_ref, "owner_role": t.owner_role,
                 "owner_recipients": int(counts.get(t.owner_role, 0)) if t.owner_role else 0,
                 "requires_approval": t.requires_approval, "sla_days": t.sla_days,
                 "enabled": t.enabled, "note": t.note,
                 "has_handler": rec.get_handler(t.handler_ref) is not None} for t in rows]

    async def list_runs(self, session: AsyncSession, *, code: str, limit: int = 50) -> list[dict]:
        rows = (await session.scalars(
            select(RecurringTaskRun).where(RecurringTaskRun.task_code == code)
            .order_by(RecurringTaskRun.scheduled_for.desc()).limit(limit))).all()
        def _iso(dt):   # timestamptz 라운드트립 TZ 라벨 제거 → KST 벽시계 표기 일관(P2-7)
            dt = as_kst_wallclock(dt)
            return dt.isoformat() if dt else None
        return [{"period_key": r.period_key, "status": r.status,
                 "scheduled_for": _iso(r.scheduled_for), "started_at": _iso(r.started_at),
                 "finished_at": _iso(r.finished_at),
                 "triggered_by": r.triggered_by, "actor": r.actor, "result": r.result,
                 "error": r.error, "duration_ms": r.duration_ms} for r in rows]

    async def upsert_task(self, session: AsyncSession, *, code: str, actor: int,
                          **fields) -> RecurringTask:
        """정의 등록/수정 — 화면·시드에서 사용. 허용 필드만 반영(멱등: code 기준)."""
        allowed = {"name", "domain", "category", "cadence", "anchor", "execution_mode",
                   "handler_ref", "owner_role", "requires_approval", "sla_days",
                   "source_ref", "enabled", "note"}
        vals = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "cadence" in vals and vals["cadence"] not in rec.CADENCES:
            raise ValidationError(f"주기 오류: {vals['cadence']}")
        if "execution_mode" in vals and vals["execution_mode"] not in rec.EXEC_MODES:
            raise ValidationError(f"실행모드 오류: {vals['execution_mode']}")
        task = await session.scalar(select(RecurringTask).where(RecurringTask.code == code))
        if task is None:
            task = RecurringTask(code=code, created_by=actor,
                                 name=vals.pop("name", code), domain=vals.pop("domain", "common"),
                                 cadence=vals.pop("cadence", "MONTHLY"))
            for k, v in vals.items():
                setattr(task, k, v)
            session.add(task)
        else:
            for k, v in vals.items():
                setattr(task, k, v)
        await session.commit()
        return task

    # 코드가 정의하는 정기업무(시드 원본) — 화면의 '정의 동기화'와 배포 시드가 같은 소스를 본다.
    _STARTER = [
        # code, name, domain, cadence, category, owner_role, sla, anchor
        ("ops.ar_review_weekly", "주간 미수금 점검", "patient", "WEEKLY", "RECURRING",
         "FINANCE_MANAGER", 3, {"basis": "PERIOD_START", "offset_days": 1}),
        ("hr.four_insurance_report", "4대보험 신고", "hr", "MONTHLY", "FIXED",
         "ADMIN", 5, {"basis": "PERIOD_END", "offset_days": 15}),
        ("hr.withholding_tax", "원천세 신고·납부", "hr", "MONTHLY", "FIXED",
         "ADMIN", 3, {"basis": "PERIOD_END", "offset_days": 10}),
        ("scm.stock_count_monthly", "월 재고 실사(수불 마감)", "scm", "MONTHLY", "RECURRING",
         "LOGISTICS_MANAGER", 5, {"basis": "PERIOD_END"}),
        ("finance.month_end_close", "월 결산 마감", "finance", "MONTHLY", "FIXED",
         "ADMIN", 7, {"basis": "PERIOD_END", "offset_days": 5}),
        ("tax.vat_quarterly", "부가가치세 신고", "tax", "QUARTERLY", "FIXED",
         "ADMIN", 5, {"basis": "PERIOD_END", "offset_days": 25}),
        ("regulatory.radiation_safety", "분기 방사선 안전 점검·교육", "regulatory", "QUARTERLY",
         "FIXED", "MANAGER", 14, {"basis": "PERIOD_START", "offset_days": 14}),
        ("regulatory.committee_meeting", "정기 위원회(감염·환자안전)", "regulatory", "QUARTERLY",
         "RECURRING", "MANAGER", 14, {"basis": "PERIOD_START", "offset_days": 20}),
        ("hr.year_end_settlement", "연말정산", "hr", "YEARLY", "FIXED",
         "ADMIN", 30, {"basis": "PERIOD_START", "offset_days": 30}),
        # 다음 해 법정 요율 등록(12월 고시 후) — 미등록이면 1월 1일 급여·원천징수가 멈춘다. 2026은 전년 복사로 끝나
        # 7월 급여가 낮은 요율로 지급됐다(2026-10-10) — 체크리스트: 고시 원문 대조·'7월분부터' 상·하한 포함.
        ("hr.statutory_params_next_year", "이듬해분 4대보험 요율·소득세 파라미터 등록(고시 대조·2026 기간=2027년분)", "hr",
         "YEARLY", "FIXED", "ADMIN", 20, {"basis": "PERIOD_END", "offset_days": -20}),
        # 연간 법정·결산 업무(2026-10-10 연말 대비) — 기간 키=해당 연도. 1~3월 업무는 '그 해' 기간에 속한다
        # (예: 2027 기간의 사업장현황신고=2027-02-10·2026 귀속분 신고).
        ("common.holidays_next_year", "이듬해 공휴일 등록(설·추석·대체공휴일·선거일)", "common",
         "YEARLY", "FIXED", "ADMIN", 30, {"basis": "PERIOD_END", "offset_days": -30}),
        ("hr.employer_insurance_rates", "사업주 고용·산재보험 요율 확인(고시 대조·급여 설정)", "hr",
         "YEARLY", "FIXED", "ADMIN", 15, {"basis": "PERIOD_START", "offset_days": 14}),
        ("tax.business_status_report", "사업장현황신고(면세사업자·전년 귀속)", "tax",
         "YEARLY", "FIXED", "ADMIN", 10, {"basis": "PERIOD_START", "offset_days": 40}),
        ("tax.payment_statements", "근로·퇴직소득 지급명세서 제출(전년 귀속)", "tax",
         "YEARLY", "FIXED", "ADMIN", 10, {"basis": "PERIOD_START", "offset_days": 68}),
        ("hr.insurance_total_wage_report", "고용·산재 보수총액신고(전년 귀속)", "hr",
         "YEARLY", "FIXED", "ADMIN", 10, {"basis": "PERIOD_START", "offset_days": 73}),
        ("finance.fiscal_year_close", "회계연도 결산(전년 FY·결산분개·재무제표 확정)", "finance",
         "YEARLY", "FIXED", "ADMIN", 30, {"basis": "PERIOD_START", "offset_days": 44}),
    ]
    _MIGRATED = [
        # code, name, domain, handler_ref, cadence, anchor(원 스케줄 시각)
        ("scm.contract_lifecycle", "위수탁계약 만기 자동전이", "scm",
         "scm.contract_lifecycle", "DAILY", {"hour": 4, "minute": 45}),
        ("hr.annual_leave_grant", "연차 자동부여", "hr",
         "hr.annual_leave_grant", "DAILY", {"hour": 2, "minute": 15}),
        ("patient.accrual", "본인부담 발생주의 미수금", "patient",
         "patient.accrual", "DAILY", {"hour": 3, "minute": 15}),
        ("claims.prereview_sync", "사전심사 일일 sync", "claims",
         "claims.prereview_sync", "DAILY", {"hour": 2, "minute": 0}),
        ("finance.balance_checkpoint", "잔액 체크포인트·검증", "finance",
         "finance.balance_checkpoint", "DAILY", {"hour": 4, "minute": 25}),
        ("finance.ledger_integrity", "원장 무결성 검증", "finance",
         "finance.ledger_integrity", "DAILY", {"hour": 4, "minute": 10}),
        # 도메인 경계 불변식 — 사람이 화면을 열지 않아도 이상이 닿게 한다(정상이면 무알림)
        ("ops.consistency_audit", "운영 정합성 점검", "common",
         "ops.consistency_audit", "DAILY", {"hour": 4, "minute": 40}),
        ("finance.budget_alerts", "예산 초과 경보", "finance",
         "finance.budget_alerts", "DAILY", {"hour": 5, "minute": 45}),
        ("common.approval_ledger_verify", "결재원장 무결성 검증", "common",
         "common.approval_ledger_verify", "DAILY", {"hour": 4, "minute": 15}),
        ("hr.license_expiry", "면허 만료·보수교육 경보", "hr",
         "hr.license_expiry", "DAILY", {"hour": 5, "minute": 0}),
        # 신규 AUTO — 퇴사자 ERP 계정 차단
        ("hr.resigned_access_revoke", "퇴사자 ERP 계정 차단", "hr",
         "hr.resigned_access_revoke", "DAILY", {"hour": 0, "minute": 20}),
        ("analytics.bi_marts", "BI 마트 야간 일괄 갱신", "analytics",
         "analytics.bi_marts", "DAILY", {"hour": 3, "minute": 50}),
        # 월간 이관(구 _startup_catchup 대체 — 러너 catchup·기간멱등이 재시작 보정 담당)
        ("itam.cost_review", "IT 비용 월간 점검", "itam",
         "itam.cost_review", "MONTHLY", {"basis": "PERIOD_END", "offset_days": 3, "hour": 5}),
        ("common.retention_sweep", "보존연한 경과 데이터 식별", "common",
         "common.retention_sweep", "MONTHLY", {"hour": 4, "minute": 30}),
        # 이관 아님·신규 AUTO — 주간 운영 브리핑(M4 백로그·월요일 07시 앵커→메타잡 07:20 실행)
        ("common.weekly_briefing", "정기업무 주간 브리핑", "common",
         "common.weekly_briefing", "WEEKLY", {"hour": 7}),
    ]
    #: 코드 정의와 비교하는 필드 — enabled·note·source_ref는 운영 상태라 비교하지 않는다.
    REGISTRY_FIELDS = ("name", "domain", "cadence", "category", "execution_mode", "handler_ref",
                       "owner_role", "sla_days", "anchor")

    def registry_defs(self) -> dict[str, dict]:
        """코드가 정의하는 정기업무 전체 — code → 필드(REGISTRY_FIELDS)."""
        out: dict[str, dict] = {}
        for code, name, domain, cadence, category, owner, sla, anchor in self._STARTER:
            # 기본은 MANUAL. AI 보조 도입분만 예외로 모드·핸들러를 지정한다.
            mode, handler = self._AI_ASSISTED.get(code, ("MANUAL", None))
            out[code] = {"name": name, "domain": domain, "cadence": cadence, "category": category,
                         "execution_mode": mode, "handler_ref": handler, "owner_role": owner,
                         "sla_days": sla, "anchor": anchor}
        for code, name, domain, handler_ref, cadence, anchor in self._MIGRATED:
            out[code] = {"name": name, "domain": domain, "cadence": cadence,
                         "category": "RECURRING", "execution_mode": "AUTO",
                         "handler_ref": handler_ref, "owner_role": "ADMIN", "sla_days": 1,
                         "anchor": anchor}
        return out

    async def _seed(self, session: AsyncSession, codes, *, actor: int) -> int:
        """코드 정의 중 **레지스트리에 없는 것만** 등록한다 — 기존 행은 건드리지 않는다.

        2026-10-10: 종전 시드는 upsert라 기존 행을 덮어써, 관리자가 화면에서 바꾼 담당 역할·SLA·기준일이
        배포 때마다(런북의 `recurring_cli seed`) 코드값으로 조용히 되돌아갔다(감사 기록도 없음). 이제 시드는
        누락분 추가만 하고, 코드 정의가 바뀐 기존 업무는 관리화면의 '정의 동기화'에서 관리자가 골라 반영한다.
        """
        defs = self.registry_defs()
        existing = set((await session.scalars(
            select(RecurringTask.code).where(RecurringTask.code.in_(list(codes))))).all())
        for code in codes:
            if code not in existing:
                await self.upsert_task(session, code=code, actor=actor, **defs[code])
        return len(codes)

    async def seed_starter_tasks(self, session: AsyncSession, *, actor: int = SYSTEM_ACTOR) -> int:
        """초기 정기업무(수기 추적) 시드 — 실제 병원 주/월/분기 프로세스. 멱등(code 기준·재실행 안전).

        전부 MANUAL(핸들러 없음)이라 자동실행되지 않고 체크리스트/알림으로만 추적된다(AI 보조 도입분 제외).
        누락분만 등록 — 기존 행(관리화면 편집 포함)은 보존한다(`_seed`).
        """
        return await self._seed(session, [c[0] for c in self._STARTER], actor=actor)

    async def seed_migrated_jobs(self, session: AsyncSession, *, actor: int = SYSTEM_ACTOR) -> int:
        """레지스트리로 컷오버된 스케줄러 AUTO 잡의 정의 시드 — 핸들러는 erp.app.recurring_handlers.

        신규는 모델기본 enabled=True(컷오버 상태)·기존 행은 보존(`_seed`).
        APScheduler add_job은 제거됨 — job_recurring이 유일 실행경로(이중실행 없음).
        """
        return await self._seed(session, [c[0] for c in self._MIGRATED], actor=actor)

    async def _kept(self, session: AsyncSession) -> dict[str, dict]:
        """관리자가 '현재 값 유지'로 확인한 코드 정의(업무별 최신 REGISTRY_KEEP 감사의 after)."""
        from erp.audit.models import AuditLog
        rows = (await session.execute(
            select(AuditLog.target_id, AuditLog.before, AuditLog.after).where(
                AuditLog.entity == "common.recurring_tasks", AuditLog.action == "REGISTRY_KEEP")
            .order_by(AuditLog.id))).all()
        # after=확인 당시 코드 정의 · before=확인 당시 레지스트리 값(+reason)
        return {code: {"code": after, "registry": {k: v for k, v in (before or {}).items()
                                                   if k != "reason"}}
                for code, before, after in rows}

    async def registry_drift(self, session: AsyncSession) -> dict:
        """코드 정의 ↔ 레지스트리 차이 — 화면 '정의 동기화' 패널(읽기 전용).

        missing=코드에만 있음(신규 업무 미등록) · differs=필드 값이 다름(코드 변경 또는 관리자 편집 —
        어느 쪽인지는 판단 불가라 관리자가 고른다) · extra=레지스트리에만 있음(화면 등록·폐기된 정의).
        관리자가 '현재 값 유지'로 확인한 차이는 kept로 빠진다 — 단, 코드 정의가 그 뒤 다시 바뀌면 다시 보인다
        (확인은 그 시점의 코드 정의에 대한 것이라서).
        """
        defs = self.registry_defs()
        kept = await self._kept(session)
        rows = {t.code: t for t in (await session.scalars(select(RecurringTask))).all()}
        missing = [{"code": c, **d} for c, d in defs.items() if c not in rows]
        differs, kept_codes = [], []
        for c, d in defs.items():
            t = rows.get(c)
            if t is None:
                continue
            fields = [{"field": f, "registry": getattr(t, f), "code": d[f]}
                      for f in self.REGISTRY_FIELDS if getattr(t, f) != d[f]]
            if not fields:
                continue
            k = kept.get(c)
            # 확인은 '그때의 코드 정의 + 그때의 레지스트리 값' 조합에만 유효 — 이후 화면에서 다른 필드를 또 바꾸면
            # 아무도 확인하지 않은 값이므로 다시 보인다(2차 검토)
            if k and k["code"] == d and k["registry"] == {f: getattr(t, f) for f in self.REGISTRY_FIELDS}:
                kept_codes.append(c)
                continue
            differs.append({"code": c, "name": t.name, "fields": fields})
        extra = [{"code": c, "name": t.name, "enabled": t.enabled}
                 for c, t in rows.items() if c not in defs]
        return {"missing": missing, "differs": differs, "extra": extra, "kept": kept_codes,
                "in_sync": not missing and not differs}

    async def registry_apply(self, session: AsyncSession, *, codes: list[str],
                             actor: int) -> dict:
        """선택한 업무를 코드 정의로 맞춘다(누락=등록·다름=코드값 반영) — enabled는 보존. 감사 기록.

        upsert_task를 쓰지 않는다 — 그건 None을 걸러 MANUAL 정의의 handler_ref=None을 반영할 수 없고(차이가
        영원히 안 닫힘), 건마다 커밋한다. 여기서는 정의 필드를 None까지 그대로 쓰고 마지막에 한 번 커밋한다.
        """
        from erp.audit.service import AuditService
        defs = self.registry_defs()
        unknown = [c for c in codes if c not in defs]
        if unknown:
            raise ValidationError(f"코드 정의에 없는 업무: {', '.join(unknown)}")
        applied = []
        for code in dict.fromkeys(codes):
            t = await session.scalar(select(RecurringTask).where(RecurringTask.code == code))
            before = None if t is None else {f: getattr(t, f) for f in self.REGISTRY_FIELDS}
            if before == defs[code]:
                continue
            if t is None:
                t = RecurringTask(code=code, created_by=actor, name=defs[code]["name"],
                                  domain=defs[code]["domain"], cadence=defs[code]["cadence"])
                session.add(t)
            for f in self.REGISTRY_FIELDS:
                setattr(t, f, defs[code][f])
            await AuditService().record_audit(
                session, actor=actor, action="REGISTRY_SYNC", entity="common.recurring_tasks",
                target_id=code, before=before, after=defs[code])
            applied.append(code)
        await session.commit()        # 한 번에 확정 — 이후 DB 접근 없음
        return {"applied": applied}

    async def registry_keep(self, session: AsyncSession, *, code: str, actor: int,
                            reason: str) -> dict:
        """코드 정의와 다른 현재 값을 의도된 것으로 확인 — 차이 목록에서 뺀다(감사 REGISTRY_KEEP)."""
        from erp.audit.service import AuditService
        defs = self.registry_defs()
        if code not in defs:
            raise ValidationError(f"코드 정의에 없는 업무: {code}")
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("현재 값을 유지하는 이유를 입력하세요")
        t = await session.scalar(select(RecurringTask).where(RecurringTask.code == code))
        if t is None:
            raise ValidationError("레지스트리에 없는 업무입니다 — '등록'하세요")
        await AuditService().record_audit(
            session, actor=actor, action="REGISTRY_KEEP", entity="common.recurring_tasks",
            target_id=code, before={"reason": reason,
                                    **{f: getattr(t, f) for f in self.REGISTRY_FIELDS}},
            after=defs[code])
        await session.commit()
        return {"code": code, "kept": True}

    async def due_list(self, session: AsyncSession, *,
                       now: datetime | None = None) -> list[dict]:
        """AI CLI/화면용 현황 — enabled 업무의 현재 기간 상태·overdue·ai_runnable(읽기전용)."""
        now = now or rec.now_naive()
        today = now.date()
        tasks = (await session.scalars(
            select(RecurringTask).where(RecurringTask.enabled.is_(True))
            .order_by(RecurringTask.cadence, RecurringTask.code))).all()
        out = []
        for task in tasks:
            pkey = rec.period_key(task.cadence, today)
            sched = rec.scheduled_for(task.cadence, today, task.anchor)
            run = await self._get_run(session, task.code, pkey)
            if run is None and _registered_after_due(task, sched):
                status = "NOT_APPLICABLE"            # 등록 전에 기한이 지난 기간(run_due와 같은 규칙) — 지연 아님
            else:
                status = run.status if run is not None else ("DUE" if now >= sched else "SCHEDULED")
            overdue = (status in ("DUE", "WAITING_APPROVAL")
                       and now > sched + timedelta(days=task.sla_days))
            out.append({
                "code": task.code, "name": task.name, "domain": task.domain,
                "cadence": task.cadence, "execution_mode": task.execution_mode,
                "period_key": pkey, "scheduled_for": sched.isoformat(),
                "status": status, "overdue": overdue,
                "ai_runnable": task.execution_mode == "AUTO" and not task.requires_approval,
                "owner_role": task.owner_role})
        return out
