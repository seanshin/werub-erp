"""사전심사 학습 사례 — 자동수확(라벨·의료진확인)·수동등록·승인·적격·내보내기."""
from datetime import date

import pytest

from erp.claims.learning_service import LearningCaseService
from erp.claims.models import (
    PreReviewCase, PreReviewFinding, PreReviewQuery, PreReviewRule,
)
from erp.errors import ValidationError

svc = LearningCaseService()


async def _case_with_decided_finding(session, *, decision, rule_code="R-DUP",
                                     rule_type="DUPLICATE", gated=False, chart="C-L1"):
    session.add(PreReviewRule(rule_type=rule_type, code=rule_code, name=rule_type,
                              severity="WARN", physician_gated=gated))
    c = PreReviewCase(chart_no=chart, care_month="2026-06", status="IN_REVIEW",
                      encounter_type="OUTPATIENT")
    session.add(c)
    await session.flush()
    f = PreReviewFinding(case_id=c.id, rule_code=rule_code, rule_name=rule_type,
                         severity="WARN", code="AA1", amount=12_000, decision=decision,
                         decision_note="결정 사유 기록", treated_on=date(2026, 6, 1))
    session.add(f)
    await session.flush()
    return c, f


async def test_harvest_labels_from_decision(session):
    """자동수확 — ACCEPT→REDUCTION, DISMISS→FALSE_POSITIVE. 의료진 확인 없으면 DRAFT."""
    await _case_with_decided_finding(session, decision="ACCEPT", chart="C-A")
    await _case_with_decided_finding(session, decision="DISMISS", rule_code="R-CNT",
                                     rule_type="COUNT_LIMIT", chart="C-D")
    out = await svc.harvest(session, actor=1)
    assert out["added"] == 2
    lst = await svc.list_cases(session)
    labels = {i["label"] for i in lst["items"]}
    assert labels == {"REDUCTION", "FALSE_POSITIVE"}
    assert all(i["quality"] == "DRAFT" for i in lst["items"])     # 의료진 확인 없음 → DRAFT
    # 재수확 멱등 — 중복 없음
    assert (await svc.harvest(session, actor=1))["added"] == 0


async def test_harvest_excludes_auto_decided(session):
    """P2 자동오탐(auto_decided)은 학습 수확 제외 — 자동→자동 순환 방지."""
    _c, f = await _case_with_decided_finding(session, decision="DISMISS")
    f.auto_decided = True
    await session.flush()
    assert (await svc.harvest(session, actor=1))["added"] == 0


async def test_physician_confirmed_auto_approved_and_eligible(session):
    """의료진 확인된 결정 → 수확 시 즉시 APPROVED·학습 적격(eligible)."""
    c, f = await _case_with_decided_finding(session, decision="DISMISS", gated=True,
                                            rule_code="R-EXCL", rule_type="EXCLUSIVE_PAIR")
    session.add(PreReviewQuery(case_id=c.id, finding_id=f.id, question="근거?",
                               raised_by=1, assigned_to=55, confirmed_by_employee=55,
                               status="ACCEPTED"))
    await session.flush()
    await svc.harvest(session, actor=1)
    items = (await svc.list_cases(session))["items"]
    lc = items[0]
    assert lc["physician_confirmed"] and lc["quality"] == "APPROVED" and lc["eligible"]
    # 적격만 내보내기 — 1건
    assert len(await svc.export(session)) == 1


async def test_gated_case_not_eligible_without_physician(session):
    """의학판단 룰 학습 사례는 의료진 확인 없으면 승인돼도 학습 부적격(고품질 라벨 보장)."""
    await _case_with_decided_finding(session, decision="DISMISS", gated=True,
                                     rule_code="R-PER", rule_type="PERIOD_LIMIT")
    await svc.harvest(session, actor=1)
    lc_id = (await svc.list_cases(session))["items"][0]["id"]
    await svc.update(session, actor=1, case_id=lc_id, quality="APPROVED")  # 승인해도
    items = (await svc.list_cases(session))["items"]
    assert items[0]["quality"] == "APPROVED" and items[0]["eligible"] is False  # 의료진 확인 없음
    assert len(await svc.export(session)) == 0


async def test_manual_register_and_dup_guard(session):
    """수동 등록(적발 연결) + 동일 적발 중복 차단."""
    _c, f = await _case_with_decided_finding(session, decision="DISMISS")
    lc = await svc.create(session, actor=1, label="FALSE_POSITIVE",
                          rationale="수기 근거", source_finding_id=f.id)
    assert lc.source == "MANUAL" and lc.label == "FALSE_POSITIVE"
    with pytest.raises(ValidationError, match="이미 등록"):
        await svc.create(session, actor=1, label="FALSE_POSITIVE", source_finding_id=f.id)


async def test_reduction_without_rationale_eligible(session):
    """삭감위험(인정=ACCEPT)은 사유 미기재여도 승인 시 학습 적격 — 라벨이 핵심 신호."""
    session.add(PreReviewRule(rule_type="DUPLICATE", code="R-RED", name="DUP", severity="WARN"))
    c = PreReviewCase(chart_no="C-RED", care_month="2026-06", status="IN_REVIEW")
    session.add(c)
    await session.flush()
    f = PreReviewFinding(case_id=c.id, rule_code="R-RED", severity="WARN", code="X1",
                         amount=1000, decision="ACCEPT", decision_note=None)
    session.add(f)
    await session.flush()
    await svc.harvest(session, actor=1)
    lc_id = (await svc.list_cases(session))["items"][0]["id"]
    await svc.update(session, actor=1, case_id=lc_id, quality="APPROVED")
    assert (await svc.list_cases(session))["items"][0]["eligible"] is True
    assert len(await svc.export(session)) == 1


async def test_false_positive_without_rationale_not_eligible(session):
    """오탐(FALSE_POSITIVE)은 사유 없으면 승인돼도 부적격 — 왜 오탐인지가 학습 컨텍스트."""
    session.add(PreReviewRule(rule_type="DUPLICATE", code="R-FP", name="DUP", severity="WARN"))
    c = PreReviewCase(chart_no="C-FP", care_month="2026-06", status="IN_REVIEW")
    session.add(c)
    await session.flush()
    f = PreReviewFinding(case_id=c.id, rule_code="R-FP", severity="WARN", code="X2",
                         amount=1000, decision="DISMISS", decision_note=None)
    session.add(f)
    await session.flush()
    await svc.harvest(session, actor=1)
    lc_id = (await svc.list_cases(session))["items"][0]["id"]
    await svc.update(session, actor=1, case_id=lc_id, quality="APPROVED")
    assert (await svc.list_cases(session))["items"][0]["eligible"] is False


async def test_invalid_label_rejected(session):
    with pytest.raises(ValidationError, match="label"):
        await svc.create(session, actor=1, label="WRONG")


# ── 부서경계 IDOR/PII (마이그 0153·심층 재검토 보완) ─────────────────────
async def test_harvest_and_list_carry_dept_code_and_filter(session):
    """학습 사례가 원 케이스 dept_code를 보존하고, 부서경계 목록/내보내기가 본부서만 노출."""
    c, f = await _case_with_decided_finding(session, decision="ACCEPT", chart="C-OS")
    c.dept_code = "OS"                       # 원 케이스 부서
    await session.flush()
    await svc.harvest(session, actor=1)
    # dept_code 보존 확인
    items = (await svc.list_cases(session))["items"]
    assert items[0]["dept_code"] == "OS"
    # 부서경계 — 타부서(IM)엔 비노출, 본부서(OS)·전역(None)엔 노출
    assert (await svc.list_cases(session, dept_codes=["IM"]))["items"] == []
    assert len((await svc.list_cases(session, dept_codes=["OS"]))["items"]) == 1
    assert len((await svc.list_cases(session, dept_codes=None))["items"]) == 1


async def test_learning_router_dept_boundary_idor(session):
    """라우터 — 학습 목록·수정·생성·내보내기 부서경계(타부서 PII 노출·개변·취입 차단)."""
    from erp.claims.router import (
        LearningCreateBody, create_learning_case,
        export_learning_cases, list_learning_cases, update_learning_case,
    )
    from erp.common.models import Department
    from erp.common.router import CurrentUser
    from erp.errors import ForbiddenError
    os_d = Department(name="정형외과", code="OS")
    im = Department(name="내과", code="IM")
    session.add_all([os_d, im])
    await session.flush()

    def _mgr(dept_id):
        return CurrentUser(user_id=100 + dept_id, role="MANAGER", dept_id=dept_id,
                           license_flag=True)
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)

    c, f = await _case_with_decided_finding(session, decision="DISMISS", chart="C-OS2")
    c.dept_code = "OS"
    await session.flush()
    await svc.harvest(session, actor=1)
    lc_id = (await svc.list_cases(session))["items"][0]["id"]
    await svc.update(session, actor=1, case_id=lc_id, quality="APPROVED")

    # 목록 — 타부서(IM) 비노출, 본부서(OS)·ADMIN 노출
    assert (await list_learning_cases(user=_mgr(im.id), session=session))["items"] == []
    assert len((await list_learning_cases(user=_mgr(os_d.id), session=session))["items"]) == 1
    assert len((await list_learning_cases(user=admin, session=session))["items"]) == 1

    # 내보내기(반출) — 타부서(IM)엔 빈 JSONL, ADMIN엔 1건(PII 반출 부서경계)
    im_exp = await export_learning_cases(user=_mgr(im.id), session=session)
    admin_exp = await export_learning_cases(user=admin, session=session)
    im_body = "".join([c async for c in im_exp.body_iterator])
    admin_body = "".join([c async for c in admin_exp.body_iterator])
    assert im_body == "" and admin_body.strip()   # 타부서 반출 없음·ADMIN 반출 있음

    # 수정 — 타부서 개변 차단
    with pytest.raises(ForbiddenError, match="타 부서"):
        await update_learning_case(case_id=lc_id, body=_lub(quality="REJECTED"),
                                   user=_mgr(im.id), session=session)

    # 생성 — 타부서 적발 취입 차단
    with pytest.raises(ForbiddenError, match="타 부서"):
        await create_learning_case(
            body=LearningCreateBody(label="REDUCTION", source_finding_id=f.id),
            user=_mgr(im.id), session=session)


def _lub(**kw):
    from erp.claims.router import LearningUpdateBody
    return LearningUpdateBody(**kw)
