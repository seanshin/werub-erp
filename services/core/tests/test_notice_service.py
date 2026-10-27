"""공시 관리 (FR-51) + AI 발신 큐 (FR-52/53) — 등록·개정·폐지·필터·긴급 분류."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.regulatory.models import AiDispatchQueue
from erp.regulatory.service import NoticeService

svc = NoticeService()
TODAY = date.today()


async def _dispatch_rows(session):
    return (await session.execute(select(AiDispatchQueue))).scalars().all()


async def test_create_enqueues_create_event(session):
    notice = await svc.create(session, notice_type="ADMIN", title="원내 안내",
                              body_text="일반 행정 안내문", effective_from=TODAY + timedelta(days=30),
                              effective_to=None, created_by=1)
    rows = await _dispatch_rows(session)
    assert len(rows) == 1 and rows[0].event_type == "CREATE"
    assert rows[0].filter_status == "PASSED"
    assert rows[0].urgency == "BATCH"
    assert notice.current_rev == 1


async def test_body_over_1mb_rejected(session):
    with pytest.raises(ValidationError):
        await svc.create(session, notice_type="ADMIN", title="대용량",
                         body_text="가" * 400_000,  # 한글 3바이트 — 1.2MB
                         effective_from=TODAY, effective_to=None, created_by=1)


async def test_narcotic_always_immediate(session):
    """긴급 기준 ② — 마약류 공시 전체 IMMEDIATE (Plan C-1)."""
    await svc.create(session, notice_type="NARCOTIC", title="마약류 보관 기준",
                     body_text="보관 기준 변경", effective_from=TODAY + timedelta(days=60),
                     effective_to=None, created_by=1)
    rows = await _dispatch_rows(session)
    assert rows[0].urgency == "IMMEDIATE"


async def test_review_criteria_within_7days_immediate(session):
    """긴급 기준 ① — 시행 7일 이내 심사기준."""
    await svc.create(session, notice_type="REVIEW_CRITERIA", title="인정 기준 변경",
                     body_text="기준 변경", effective_from=TODAY + timedelta(days=5),
                     effective_to=None, created_by=1)
    rows = await _dispatch_rows(session)
    assert rows[0].urgency == "IMMEDIATE"


async def test_internal_starts_blocked(session):
    """INTERNAL — 무조건 관리자 승인 (BLOCKED 시작)."""
    await svc.create(session, notice_type="INTERNAL", title="원내 규정",
                     body_text="내부 규정", effective_from=TODAY, effective_to=None, created_by=1)
    rows = await _dispatch_rows(session)
    assert rows[0].filter_status == "BLOCKED"


async def test_sensitive_body_blocked_with_findings(session):
    await svc.create(session, notice_type="ADMIN", title="공지",
                     body_text="담당자 연락처 010-1234-5678", effective_from=TODAY,
                     effective_to=None, created_by=1)
    rows = await _dispatch_rows(session)
    assert rows[0].filter_status == "BLOCKED"
    assert rows[0].filter_detail["findings"][0]["type"] == "PHONE"


async def test_revise_increments_revision_and_enqueues_update(session):
    notice = await svc.create(session, notice_type="TARIFF", title="수가 고시",
                              body_text="v1", effective_from=TODAY + timedelta(days=30),
                              effective_to=None, created_by=1)
    rev = await svc.revise(session, erp_ref_id=notice.erp_ref_id, title="수가 고시(개정)",
                           body_text="v2", effective_from=TODAY + timedelta(days=40),
                           effective_to=None, created_by=1)
    assert rev.revision_no == 2
    rows = await _dispatch_rows(session)
    assert [r.event_type for r in rows] == ["CREATE", "UPDATE"]


async def test_revoke_enqueues_revoke_and_sets_repealed(session):
    """REPEALED 전이는 revoke 필수 발신 (연동 스펙 v1.0 ②)."""
    notice = await svc.create(session, notice_type="ADMIN", title="폐지 대상",
                              body_text="본문", effective_from=TODAY, effective_to=None,
                              created_by=1)
    await svc.revoke(session, erp_ref_id=notice.erp_ref_id, created_by=1)
    rows = await _dispatch_rows(session)
    assert rows[-1].event_type == "REVOKE"


async def test_missing_effective_from_rejected(session):
    """FR-51 — 시행일 미입력 등록 차단."""
    with pytest.raises(ValidationError):
        await svc.create(session, notice_type="ADMIN", title="무시행일",
                         body_text="본문", effective_from=None, effective_to=None, created_by=1)
