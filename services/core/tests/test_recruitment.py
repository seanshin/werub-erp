"""채용 ATS (P3-9) — 공고·지원자·전형단계·퍼널·종결가드·검증."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.hr.recruit_service import RecruitService

svc = RecruitService()


async def _posting(session, title="간호사 채용"):
    return await svc.create_posting(session, title=title, posted_on=date(2026, 7, 1),
                                    headcount=2, actor=1)


async def test_posting_create_list_status(session):
    p = await _posting(session)
    assert p.status == "OPEN"
    rows = await svc.list_postings(session)
    assert any(x["id"] == p.id and x["applicant_count"] == 0 for x in rows)
    await svc.set_posting_status(session, posting_id=p.id, status="CLOSED")
    assert (await svc.list_postings(session, status="CLOSED"))[0]["id"] == p.id


async def test_posting_guards(session):
    with pytest.raises(ValidationError):
        await svc.create_posting(session, title=" ", posted_on=date(2026, 7, 1))
    with pytest.raises(ValidationError):
        await svc.create_posting(session, title="x", posted_on=date(2026, 7, 1), headcount=0)


async def test_applicant_and_funnel(session):
    p = await _posting(session)
    a1 = await svc.add_applicant(session, posting_id=p.id, name="지원자A",
                                 applied_on=date(2026, 7, 2), contact="010-0000-0000")
    await svc.add_applicant(session, posting_id=p.id, name="지원자B", applied_on=date(2026, 7, 3))
    assert a1.stage == "APPLIED"
    apps = await svc.list_applicants(session, posting_id=p.id)
    assert len(apps) == 2
    # 전형 진행
    await svc.advance(session, applicant_id=a1.id, stage="INTERVIEW")
    f = await svc.funnel(session, posting_id=p.id)
    assert f["total"] == 2 and f["by_stage"]["INTERVIEW"] == 1 and f["by_stage"]["APPLIED"] == 1
    # 공고 지원자 수 반영
    assert next(x for x in await svc.list_postings(session) if x["id"] == p.id)["applicant_count"] == 2


async def test_terminal_stage_locked(session):
    p = await _posting(session)
    a = await svc.add_applicant(session, posting_id=p.id, name="C", applied_on=date(2026, 7, 2))
    await svc.advance(session, applicant_id=a.id, stage="REJECTED")
    with pytest.raises(ValidationError):        # 종결(REJECTED) 후 변경 불가
        await svc.advance(session, applicant_id=a.id, stage="INTERVIEW")


async def test_guards(session):
    with pytest.raises(ValidationError):
        await svc.add_applicant(session, posting_id=999999, name="X", applied_on=date(2026, 7, 2))
    p = await _posting(session)
    with pytest.raises(ValidationError):
        await svc.add_applicant(session, posting_id=p.id, name=" ", applied_on=date(2026, 7, 2))
    a = await svc.add_applicant(session, posting_id=p.id, name="Y", applied_on=date(2026, 7, 2))
    with pytest.raises(ValidationError):
        await svc.advance(session, applicant_id=a.id, stage="BAD")
