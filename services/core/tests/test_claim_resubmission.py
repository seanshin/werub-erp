"""반송(반려) 청구 재청구 추적 — 반송 접수·재청구 연결·미재청구 조회·멱등."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.claims.models import ClaimResubmission
from erp.claims.service import ClaimResubmissionService
from erp.errors import ValidationError

svc = ClaimResubmissionService()


async def test_record_return_and_resubmit_flow(session):
    """반송 접수(RETURNED) → 재청구 연결(RESUBMITTED) → 종결(CLOSED)."""
    r = await svc.record_return(session, actor=1, original_ref="R-ORIG-1", care_month="2026-06",
                                returned_on=date(2026, 7, 1), return_code="A99",
                                return_reason="수진자 자격 불일치", amount=120_000)
    assert r.status == "RETURNED"
    r2 = await svc.resubmit(session, actor=1, resubmission_id=r.id,
                            resubmit_ref="R-RESUB-1", resubmitted_on=date(2026, 7, 5))
    assert r2.status == "RESUBMITTED" and r2.resubmit_ref == "R-RESUB-1"
    r3 = await svc.close(session, actor=1, resubmission_id=r.id, note="재청구 결정 반영")
    assert r3.status == "CLOSED"


async def test_record_return_idempotent(session):
    """멱등 — 동일 원 청구(original_ref) 반송 재접수 시 증식 없음."""
    a = await svc.record_return(session, actor=1, original_ref="R-DUP", care_month="2026-06",
                                returned_on=date(2026, 7, 1))
    b = await svc.record_return(session, actor=1, original_ref="R-DUP", care_month="2026-06",
                                returned_on=date(2026, 7, 1))
    assert a.id == b.id
    assert len((await session.scalars(select(ClaimResubmission))).all()) == 1


async def test_record_return_idempotent_across_whitespace(session):
    """멱등 정규화 — 공백만 다른 original_ref 재접수도 동일 반송으로 처리."""
    a = await svc.record_return(session, actor=1, original_ref="R-WS", care_month="2026-06",
                                returned_on=date(2026, 7, 1))
    b = await svc.record_return(session, actor=1, original_ref="  R-WS ", care_month="2026-06",
                                returned_on=date(2026, 7, 1))
    assert a.id == b.id
    assert len((await session.scalars(select(ClaimResubmission))).all()) == 1


async def test_record_return_input_validation(session):
    """입력 검증 — 음수 반송금액·잘못된 진료월 형식 거부(집계 왜곡·미대사 방지)."""
    with pytest.raises(ValidationError, match="음수"):
        await svc.record_return(session, actor=1, original_ref="R-NEG", care_month="2026-06",
                                returned_on=date(2026, 7, 1), amount=-100)
    with pytest.raises(ValidationError, match="진료월"):
        await svc.record_return(session, actor=1, original_ref="R-FMT", care_month="202606",
                                returned_on=date(2026, 7, 1), amount=100)


async def test_resubmit_guards(session):
    r = await svc.record_return(session, actor=1, original_ref="R-G", care_month="2026-06",
                                returned_on=date(2026, 7, 1))
    with pytest.raises(ValidationError, match="접수번호"):
        await svc.resubmit(session, actor=1, resubmission_id=r.id, resubmit_ref="  ",
                           resubmitted_on=date(2026, 7, 5))
    await svc.resubmit(session, actor=1, resubmission_id=r.id, resubmit_ref="R-2",
                       resubmitted_on=date(2026, 7, 5))
    # 이미 재청구된 건 재재청구 불가
    with pytest.raises(ValidationError, match="가능 상태"):
        await svc.resubmit(session, actor=1, resubmission_id=r.id, resubmit_ref="R-3",
                           resubmitted_on=date(2026, 7, 6))


async def test_record_return_requires_original_ref(session):
    with pytest.raises(ValidationError, match="원 청구"):
        await svc.record_return(session, actor=1, original_ref="  ", care_month="2026-06",
                                returned_on=date(2026, 7, 1))


async def test_pending_filter_and_amount_summary(session):
    """미재청구(pending) 조회 — RETURNED만 + 미재청구 총액. 재청구/종결 제외."""
    await svc.record_return(session, actor=1, original_ref="R-P1", care_month="2026-06",
                            returned_on=date(2026, 7, 1), amount=100_000)
    await svc.record_return(session, actor=1, original_ref="R-P2", care_month="2026-06",
                            returned_on=date(2026, 7, 2), amount=200_000)
    done = await svc.record_return(session, actor=1, original_ref="R-P3", care_month="2026-06",
                                   returned_on=date(2026, 7, 3), amount=50_000)
    await svc.resubmit(session, actor=1, resubmission_id=done.id, resubmit_ref="RS",
                       resubmitted_on=date(2026, 7, 4))
    out = await svc.list_resubmissions(session, pending=True)
    refs = {i["original_ref"] for i in out["items"]}
    assert refs == {"R-P1", "R-P2"}                 # 재청구완료(R-P3) 제외
    assert out["pending_amount"] == 300_000         # 미재청구 총액


async def test_router_return_resubmit_and_pending(session):
    """라우터 — 반송 접수·재청구·미재청구 목록(RBAC write/read)."""
    from erp.claims.router import (
        ClaimResubmitBody, ClaimReturnBody, list_resubmissions, record_claim_return,
        resubmit_claim,
    )
    from erp.common.router import CurrentUser
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    created = await record_claim_return(
        body=ClaimReturnBody(original_ref="RT-1", care_month="2026-06",
                             returned_on=date(2026, 7, 1), amount=80_000),
        user=admin, session=session)
    assert created["status"] == "RETURNED"
    pend = await list_resubmissions(pending=True, user=admin, session=session)
    assert pend["pending_amount"] == 80_000
    await resubmit_claim(resubmission_id=created["id"],
                         body=ClaimResubmitBody(resubmit_ref="RT-1R",
                                                resubmitted_on=date(2026, 7, 3)),
                         user=admin, session=session)
    pend2 = await list_resubmissions(pending=True, user=admin, session=session)
    assert pend2["items"] == [] and pend2["pending_amount"] == 0
