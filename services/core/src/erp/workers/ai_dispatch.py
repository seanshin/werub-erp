"""AI 발신 워커 (worker-integration) — design §5.3 파이프라인.

멱등: ai_dispatch_queue.id 상태 CAS 전이 (QUEUED→SENT) — 중복 기동 안전.
승인 hash 바인딩: 발신 직전 현재 revision hash와 approved_hash 불일치 시 재필터.
재시도 3회 지수 백오프 → DLQ + 알림. IMMEDIATE는 30초 폴링(SLA 5분).
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from erp.integrations.ai_publisher import AIIndexPublisher, NoticeEvent
from erp.regulatory import filter_gate
from erp.regulatory.models import AiDispatchQueue, Notice, NoticeRevision

logger = logging.getLogger("erp.workers.ai_dispatch")

MAX_RETRY = 3
POLL_INTERVAL_IMMEDIATE = 30  # 초 (스펙 v1.0 ①)
SLA_IMMEDIATE_SECONDS = 300
# 종결 상태 정규화 — 스펙 v1.0은 done/failed지만 서버 실구현은 "completed" 반환
# (2026-07-03 HIS 진단: completed 응답에도 미종결로 22일 재폴링·전체 트래픽 48%). 동의어 수용.
TERMINAL_DONE = frozenset({"done", "completed", "success", "succeeded", "indexed"})
TERMINAL_FAILED = frozenset({"failed", "error", "rejected", "cancelled", "canceled"})
KNOWN_IN_PROGRESS = frozenset({"pending", "running"})
POLL_BACKOFF_BASE_SECONDS = 30      # 백오프 지수 base — 첫 지연 60s(30*2^1)→120→240…(cap 1h)
POLL_BACKOFF_CAP_SECONDS = 3600     # 상한 1시간
MAX_POLL_AGE_SECONDS = 72 * 3600    # 72h 미종결이면 폴링 중단(DLQ) — 무한 루프 차단


async def claim_next(session: AsyncSession) -> AiDispatchQueue | None:
    """발신 대상 클레임 — 상태 CAS (QUEUED + PASSED/APPROVED → SENT 선점)."""
    entry = (await session.execute(
        select(AiDispatchQueue)
        .where(AiDispatchQueue.dispatch_status == "QUEUED",
               AiDispatchQueue.filter_status.in_(["PASSED", "APPROVED"]))
        .order_by(AiDispatchQueue.urgency.desc(), AiDispatchQueue.id)  # IMMEDIATE 우선
        .limit(1)
    )).scalar_one_or_none()
    if entry is None:
        return None
    claimed = (await session.execute(
        update(AiDispatchQueue)
        .where(AiDispatchQueue.id == entry.id, AiDispatchQueue.dispatch_status == "QUEUED")
        .values(dispatch_status="SENT")
        .returning(AiDispatchQueue.id)
    )).first()
    return entry if claimed else None  # CAS 실패 = 다른 워커가 선점


async def _load_revision(session: AsyncSession, entry: AiDispatchQueue) -> NoticeRevision:
    return (await session.execute(
        select(NoticeRevision).where(
            NoticeRevision.erp_ref_id == entry.erp_ref_id,
            NoticeRevision.revision_no == entry.revision_no)
    )).scalar_one()


async def _verify_approval_binding(session: AsyncSession, entry: AiDispatchQueue,
                                   rev: NoticeRevision) -> bool:
    """승인 hash 바인딩 검증 (design §5.3) — 불일치 시 재필터 후 발신 중단."""
    if entry.event_type == "REVOKE":
        return True  # 본문 미발신
    if entry.approved_hash == rev.content_hash:
        return True
    # 승인 후 본문이 재수정됨 — 재필터(실데이터 대조 주입)
    charts, emps = await filter_gate.pii_reference_sets(session)
    result = filter_gate.scan(rev.body_text, known_chart_nos=charts, known_employee_nos=emps)
    await session.execute(
        update(AiDispatchQueue).where(AiDispatchQueue.id == entry.id)
        .values(dispatch_status="QUEUED",
                filter_status="BLOCKED" if result.blocked else "PASSED",
                filter_detail={"reason": "본문 변경 — 재필터",
                               "findings": result.findings} if result.blocked else None,
                approved_hash=None if result.blocked else rev.content_hash)
    )
    logger.warning("승인 hash 불일치 — 재필터", extra={"ctx": {"queue_id": entry.id,
                   "blocked": result.blocked}})
    return False


async def dispatch_one(session: AsyncSession, publisher: AIIndexPublisher,
                       entry: AiDispatchQueue) -> bool:
    """단건 발신 — 성공 시 sent_at/response 기록, 실패 시 재시도/DLQ."""
    rev = await _load_revision(session, entry)
    if not await _verify_approval_binding(session, entry, rev):
        return False

    notice = (await session.execute(
        select(Notice).where(Notice.erp_ref_id == entry.erp_ref_id)
    )).scalar_one()

    payload = None
    if entry.event_type != "REVOKE":
        payload = {
            "erp_ref_id": str(entry.erp_ref_id), "revision_no": entry.revision_no,
            "notice_type": notice.notice_type, "title": rev.title,
            "body": rev.body_text, "content_hash": rev.content_hash,  # weve 스키마: body
            "effective_from": rev.effective_from.isoformat(),
            "effective_to": rev.effective_to.isoformat() if rev.effective_to else None,
            "status": rev.status, "client_request_id": str(entry.client_request_id),
            "mode": "immediate" if entry.urgency == "IMMEDIATE" else "nightly",  # weve 색인 모드(a)
        }
    event = NoticeEvent(event_type=entry.event_type, erp_ref_id=str(entry.erp_ref_id),
                        revision_no=entry.revision_no,
                        client_request_id=str(entry.client_request_id), payload=payload)
    try:
        result = await publisher.publish(event)
    except Exception as exc:  # noqa: BLE001 — 네트워크 계열 실패
        logger.warning("발신 예외", extra={"ctx": {"queue_id": entry.id, "err": str(exc)[:120]}})
        result = None

    now = datetime.now(timezone.utc)
    if result and result.ok:
        await session.execute(
            update(AiDispatchQueue).where(AiDispatchQueue.id == entry.id)
            .values(sent_at=now, response_code=result.status_code,
                    elapsed_ms=int((now - _aware(entry.triggered_at)).total_seconds() * 1000))
        )
        return True

    retry = entry.retry_count + 1
    if retry >= MAX_RETRY:
        await session.execute(
            update(AiDispatchQueue).where(AiDispatchQueue.id == entry.id)
            .values(dispatch_status="DLQ", retry_count=retry,
                    response_code=result.status_code if result else None)
        )
        logger.error("발신 DLQ 적재", extra={"ctx": {"queue_id": entry.id}})
        # P1-6 — DLQ 적재 시 관리자 알림(운영 가시성). 재처리는 운영자가 큐 확인 후.
        from erp.common.models import Notification, User
        for uid in (await session.execute(
                select(User.id).where(User.role == "ADMIN", User.is_active.is_(True))
                )).scalars().all():
            session.add(Notification(
                user_id=uid, type="DISPATCH_DLQ", title="AI 공시 발신 DLQ 적재",
                body=f"공시 AI 발신 {MAX_RETRY}회 실패 — 발신 큐 #{entry.id} 확인 필요"))
    else:
        # 분석 H4 — 백오프 sleep을 트랜잭션 안에서 하지 않음 (커넥션·SENT 선점 점유).
        # 재시도 대기는 호출 루프가 트랜잭션 커밋 후 수행.
        await session.execute(
            update(AiDispatchQueue).where(AiDispatchQueue.id == entry.id)
            .values(dispatch_status="QUEUED", retry_count=retry)
        )
    return False


def _backoff_values(poll_count: int, now: datetime) -> dict:
    """미종결·오류 건 지수 백오프 값 — 30s→60→120…(cap 1h). next_poll_at 전 재폴링 금지."""
    count = (poll_count or 0) + 1
    delay = min(POLL_BACKOFF_BASE_SECONDS * (2 ** count), POLL_BACKOFF_CAP_SECONDS)
    return {"poll_count": count, "next_poll_at": now + timedelta(seconds=delay)}


async def poll_sent(session: AsyncSession, publisher: AIIndexPublisher) -> None:
    """SENT 건 색인 상태 폴링 — 종결(done/completed…) → DONE·DLQ, 미종결 백오프, 72h 컷오프.

    2026-07-03 HIS 진단 반영: 서버가 "completed"를 반환하는데 "done"만 종결로 인정해
    50건이 22일간 30초마다 재폴링(전체 트래픽 48%). ① 종결 동의어 수용 ② 지수 백오프
    (next_poll_at) ③ 72h 미종결 컷오프(DLQ)로 미인지 상태에도 무한 루프 불가 보장.

    ⚠ **트랜잭션 밖 HTTP**(전체검토 2026-07-03): 대상 스냅샷을 읽어 read 트랜잭션을 즉시 커밋한 뒤
    HTTP를 트랜잭션 미보유 상태로 호출하고, 결과 update만 짧은 트랜잭션으로 일괄 반영. 서버 다운 시
    50×timeout 동안 idle-in-transaction으로 공유 PG 커넥션·xmin을 점유하던 문제 제거."""
    now = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(AiDispatchQueue).where(
            AiDispatchQueue.dispatch_status == "SENT",
            AiDispatchQueue.sent_at.isnot(None),
            or_(AiDispatchQueue.next_poll_at.is_(None),
                AiDispatchQueue.next_poll_at <= now)).limit(50)
    )).scalars().all()
    # HTTP 동안 트랜잭션 미보유 — 필요한 필드만 스냅샷 후 read 트랜잭션 해제
    targets = [(e.id, str(e.client_request_id), e.poll_count or 0,
                _aware(e.sent_at), e.urgency) for e in rows]
    await session.commit()

    updates: list[tuple[int, dict]] = []   # (queue_id, values) — 트랜잭션 밖에서 결정
    for qid, crid, poll_count, sent_at, urgency in targets:
        age = (now - sent_at).total_seconds()
        try:
            status = (await publisher.poll_status(crid)).strip().lower()
        except Exception:  # noqa: BLE001
            # 서버 영구 다운·404·응답형 오류에도 72h 컷오프 적용 — 예외 경로 무한폴링 차단.
            if age > MAX_POLL_AGE_SECONDS:
                logger.warning("색인 상태조회 72h 실패 — 폴링 중단(DLQ)",
                               extra={"ctx": {"queue_id": qid}})
                updates.append((qid, {"dispatch_status": "DLQ"}))
            else:
                updates.append((qid, _backoff_values(poll_count, now)))
            continue
        if status in TERMINAL_DONE:
            updates.append((qid, {"dispatch_status": "DONE"}))
            continue
        if status in TERMINAL_FAILED:
            updates.append((qid, {"dispatch_status": "DLQ"}))
            continue
        # 미종결(pending/running) 또는 미인지 상태
        if status not in KNOWN_IN_PROGRESS:
            logger.warning("색인 상태 미인지 — 백오프 폴링",
                           extra={"ctx": {"queue_id": qid, "status": status}})
        if age > MAX_POLL_AGE_SECONDS:
            logger.warning("색인 72h 미종결 — 폴링 중단(DLQ)",
                           extra={"ctx": {"queue_id": qid, "status": status}})
            updates.append((qid, {"dispatch_status": "DLQ"}))
            continue
        updates.append((qid, _backoff_values(poll_count, now)))
        if urgency == "IMMEDIATE" and age > SLA_IMMEDIATE_SECONDS:
            logger.warning("IMMEDIATE SLA(5분) 초과", extra={"ctx": {"queue_id": qid}})

    if updates:
        dlq = [qid for qid, v in updates if v.get("dispatch_status") == "DLQ"]
        async with session.begin():   # 짧은 write 트랜잭션 — HTTP 종료 후에만 개시
            for qid, values in updates:
                await session.execute(update(AiDispatchQueue)
                                      .where(AiDispatchQueue.id == qid).values(**values))
            if dlq:
                # 발신 경로 DLQ와 같은 가시성 — 종전엔 폴링 경로 DLQ가 로그에만 남아, 색인이
                # 실패·72h 미종결로 끝나도 관리자가 몰랐다.
                from erp.common.models import Notification, User
                ids = ", ".join(f"#{q}" for q in dlq[:10]) + (" 외" if len(dlq) > 10 else "")
                for uid in (await session.execute(
                        select(User.id).where(User.role == "ADMIN", User.is_active.is_(True))
                        )).scalars().all():
                    session.add(Notification(
                        user_id=uid, type="DISPATCH_DLQ", title="AI 공시 색인 DLQ 적재",
                        body=f"색인 실패·72시간 미종결 {len(dlq)}건 — 발신 큐 {ids} 확인 필요"))


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
