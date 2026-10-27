"""AI 발신 워커 — CAS 클레임·승인 hash 바인딩·재시도/DLQ (design §5.3)."""
from datetime import date, timedelta

from sqlalchemy import select, update

from erp.integrations.ai_publisher import PublishResult
from erp.regulatory.models import AiDispatchQueue, NoticeRevision
from erp.regulatory.service import NoticeService
from erp.workers.ai_dispatch import claim_next, dispatch_one

svc = NoticeService()
TODAY = date.today()


class FakePublisher:
    def __init__(self, ok=True, status_code=200):
        self.ok, self.status_code = ok, status_code
        self.published: list = []

    async def publish(self, event):
        self.published.append(event)
        return PublishResult(ok=self.ok, status_code=self.status_code, elapsed_ms=10)

    async def poll_status(self, client_request_id):
        return "done"

    async def sync_verify(self, snapshot):
        return []


async def _make_notice(session, body="요양급여 기준 안내문"):
    return await svc.create(session, notice_type="ADMIN", title="공지", body_text=body,
                            effective_from=TODAY + timedelta(days=30),
                            effective_to=None, created_by=1)


async def test_claim_cas_marks_sent(session):
    await _make_notice(session)
    entry = await claim_next(session)
    assert entry is not None
    again = await claim_next(session)
    assert again is None  # 이미 선점 — 중복 클레임 불가 (멱등)


async def test_dispatch_success_records_sla_fields(session):
    await _make_notice(session)
    entry = await claim_next(session)
    pub = FakePublisher()
    ok = await dispatch_one(session, pub, entry)
    assert ok and len(pub.published) == 1
    row = (await session.execute(
        select(AiDispatchQueue).where(AiDispatchQueue.id == entry.id))).scalar_one()
    assert row.sent_at is not None and row.response_code == 200
    assert pub.published[0].payload["content_hash"]  # 페이로드에 hash 포함


async def test_approval_hash_mismatch_triggers_refilter(session):
    """승인 후 본문 재수정 → 발신 중단 + 재필터 (design §5.3)."""
    notice = await _make_notice(session)
    entry = await claim_next(session)

    # 발신 전에 본문이 몰래 수정됨 (hash 불일치 시나리오) — 민감정보 주입
    await session.execute(
        update(NoticeRevision)
        .where(NoticeRevision.erp_ref_id == notice.erp_ref_id)
        .values(body_text="유출 시도 010-1234-5678", content_hash="f" * 64))

    pub = FakePublisher()
    ok = await dispatch_one(session, pub, entry)
    assert not ok and len(pub.published) == 0  # 발신 차단

    row = (await session.execute(
        select(AiDispatchQueue).where(AiDispatchQueue.id == entry.id))).scalar_one()
    assert row.dispatch_status == "QUEUED" and row.filter_status == "BLOCKED"


async def test_dispatch_failure_retries_then_dlq(session):
    # 백오프 sleep은 트랜잭션 밖(호출 루프)으로 이동 (분석 H4) — 워커 함수는 즉시 반환
    from erp.common.models import Notification, User
    session.add(User(email="adm@x.example", password_hash="x", role="ADMIN"))   # 알림 대상
    await _make_notice(session)
    pub = FakePublisher(ok=False, status_code=503)
    for _ in range(3):  # MAX_RETRY=3
        entry = await claim_next(session)
        assert entry is not None
        await dispatch_one(session, pub, entry)

    row = (await session.execute(select(AiDispatchQueue))).scalar_one()
    assert row.dispatch_status == "DLQ" and row.retry_count == 3
    # P1-6 — DLQ 적재 시 ADMIN 알림 생성(운영 가시성)
    n = (await session.execute(select(Notification)
         .where(Notification.type == "DISPATCH_DLQ"))).scalars().all()
    assert len(n) >= 1


class StatusPublisher(FakePublisher):
    """poll_status 응답을 지정 — 폴러 종결조건·백오프 테스트용."""
    def __init__(self, status: str):
        super().__init__()
        self.status = status
        self.poll_calls = 0

    async def poll_status(self, client_request_id):
        self.poll_calls += 1
        return self.status


async def _sent_entry(session):
    """SENT 상태 큐 엔트리 생성 (발신 완료 상태 재현)."""
    from datetime import datetime, timezone
    await _make_notice(session)
    entry = await claim_next(session)
    await dispatch_one(session, FakePublisher(), entry)
    await session.execute(update(AiDispatchQueue).where(AiDispatchQueue.id == entry.id)
                          .values(sent_at=datetime.now(timezone.utc)))
    return entry


async def test_poll_sent_completed_is_terminal(session):
    """서버 실구현 "completed" → DONE (2026-07-03 HIS 진단 — 22일 무한 재폴링 회귀 방지)."""
    from erp.workers.ai_dispatch import poll_sent
    entry = await _sent_entry(session)
    await poll_sent(session, StatusPublisher("completed"))
    row = (await session.execute(
        select(AiDispatchQueue).where(AiDispatchQueue.id == entry.id))).scalar_one()
    assert row.dispatch_status == "DONE"


async def test_poll_sent_error_variant_is_dlq(session):
    """실패 동의어("error") → DLQ."""
    from erp.workers.ai_dispatch import poll_sent
    entry = await _sent_entry(session)
    await poll_sent(session, StatusPublisher("error"))
    row = (await session.execute(
        select(AiDispatchQueue).where(AiDispatchQueue.id == entry.id))).scalar_one()
    assert row.dispatch_status == "DLQ"


async def test_poll_sent_pending_backs_off(session):
    """미종결(pending) → SENT 유지 + next_poll_at 미래로(지수 백오프) — 30초 해머링 방지."""
    from datetime import datetime, timezone

    from erp.workers.ai_dispatch import poll_sent
    entry = await _sent_entry(session)
    pub = StatusPublisher("pending")
    await poll_sent(session, pub)
    row = (await session.execute(
        select(AiDispatchQueue).where(AiDispatchQueue.id == entry.id))).scalar_one()
    assert row.dispatch_status == "SENT" and row.poll_count == 1
    assert row.next_poll_at is not None
    # next_poll_at 전에는 재폴링 대상에서 제외
    await poll_sent(session, pub)
    assert pub.poll_calls == 1
    # (검증 보조) SQLite는 naive 저장 — aware 변환 후 미래 확인
    npa = row.next_poll_at if row.next_poll_at.tzinfo else row.next_poll_at.replace(tzinfo=timezone.utc)
    assert npa > datetime.now(timezone.utc)


async def test_poll_sent_stale_nonterminal_cutoff_dlq(session):
    """72h 이상 미종결 → 폴링 중단(DLQ) — 미인지 상태여도 무한 루프 불가."""
    from datetime import datetime, timedelta, timezone

    from erp.workers.ai_dispatch import poll_sent
    entry = await _sent_entry(session)
    await session.execute(update(AiDispatchQueue).where(AiDispatchQueue.id == entry.id)
                          .values(sent_at=datetime.now(timezone.utc) - timedelta(hours=73)))
    await poll_sent(session, StatusPublisher("mystery-state"))
    row = (await session.execute(
        select(AiDispatchQueue).where(AiDispatchQueue.id == entry.id))).scalar_one()
    assert row.dispatch_status == "DLQ"


def test_parse_index_status_real_server_shape():
    """서버 실응답(2026-07-03 확인) — 상태는 최상위 status가 아닌 latest/jobs[].status."""
    from erp.integrations.ai_publisher import parse_index_status
    real = {"client_request_id": "8fa7fd10-…", "latest": "completed",
            "jobs": [{"action": "index", "chunks": 172, "error": None,
                      "id": 105, "status": "completed"}]}
    assert parse_index_status(real) == "completed"
    # latest 없고 jobs만 있는 변형
    assert parse_index_status({"jobs": [{"status": "running"}]}) == "running"
    # 스펙 v1.0 원형(최상위 status)도 계속 수용
    assert parse_index_status({"status": "done"}) == "done"
    # 아무 정보 없음 → pending (기존 기본값 유지)
    assert parse_index_status({}) == "pending"
    assert parse_index_status({"jobs": []}) == "pending"


class BoomPublisher(FakePublisher):
    """poll_status가 항상 예외 — 서버 다운/404 재현."""
    async def poll_status(self, client_request_id):
        raise RuntimeError("색인서버 다운")


async def test_poll_sent_exception_path_cutoff_dlq(session):
    """예외 경로에서도 72h 컷오프 → DLQ (서버 영구 다운 시 무한폴링 차단·전체검토 2026-07-03)."""
    from datetime import datetime, timedelta, timezone

    from erp.workers.ai_dispatch import poll_sent
    entry = await _sent_entry(session)
    await session.execute(update(AiDispatchQueue).where(AiDispatchQueue.id == entry.id)
                          .values(sent_at=datetime.now(timezone.utc) - timedelta(hours=73)))
    await poll_sent(session, BoomPublisher())
    row = (await session.execute(
        select(AiDispatchQueue).where(AiDispatchQueue.id == entry.id))).scalar_one()
    assert row.dispatch_status == "DLQ"


async def test_poll_sent_exception_path_recent_backs_off(session):
    """예외 경로·72h 이내 → SENT 유지 + 백오프(DLQ 아님)."""
    from erp.workers.ai_dispatch import poll_sent
    entry = await _sent_entry(session)
    await poll_sent(session, BoomPublisher())
    row = (await session.execute(
        select(AiDispatchQueue).where(AiDispatchQueue.id == entry.id))).scalar_one()
    assert row.dispatch_status == "SENT" and row.poll_count == 1 and row.next_poll_at is not None


async def test_poll_dlq_notifies_admins(session):
    """폴링 경로 DLQ도 관리자 알림 — 종전엔 로그에만 남았다(발신 경로 DLQ와 가시성 불일치)."""
    from erp.common.models import Notification, User
    from erp.workers.ai_dispatch import poll_sent
    session.add(User(email="dlq-admin@x.example", password_hash="x", role="ADMIN"))
    await session.flush()
    await _sent_entry(session)
    await poll_sent(session, StatusPublisher("error"))
    n = (await session.execute(select(Notification).where(
        Notification.type == "DISPATCH_DLQ"))).scalars().all()
    assert n and "색인" in n[0].title
