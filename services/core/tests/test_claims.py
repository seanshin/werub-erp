"""Claims — 업로드 검증(§6.7)·이의신청 기한·기각→손실 전표(F2)·통계 (FR-38/40/48)."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from erp.claims.models import ClaimAdjustment, ClaimBatch, ClaimStatement
from erp.claims.service import AppealService, ClaimStatsService, NoticeUploadService
from erp.errors import ValidationError
from erp.finance.models import Voucher, VoucherLine

TODAY = date.today()


# ── 업로드 검증 (§6.7) ──────────────────────────────────────
async def test_upload_pdf_accepted(session, tmp_path):
    svc = NoticeUploadService(storage_dir=str(tmp_path))
    staging = await svc.upload(session, actor=1, original_name="통보서.pdf",
                               content=b"%PDF-1.7 dummy")
    assert staging.status == "NEW"
    # UUID 재명명 — 원본명은 메타로만 (경로 탐색 차단)
    assert staging.stored_name != "통보서.pdf" and len(staging.stored_name) == 32
    assert (tmp_path / staging.stored_name).exists()


async def test_upload_path_traversal_neutralized(session, tmp_path):
    svc = NoticeUploadService(storage_dir=str(tmp_path))
    staging = await svc.upload(session, actor=1,
                               original_name="../../etc/passwd.pdf",
                               content=b"%PDF-1.7 dummy")
    assert "/" not in staging.stored_name  # 저장 경로 = prefix + UUID만
    assert not (tmp_path.parent.parent / "etc").exists()


async def test_upload_binary_garbage_rejected(session, tmp_path):
    svc = NoticeUploadService(storage_dir=str(tmp_path))
    with pytest.raises(ValidationError):
        await svc.upload(session, actor=1, original_name="x.exe",
                         content=bytes(range(256)) * 4)


async def test_upload_oversize_rejected(session, tmp_path):
    svc = NoticeUploadService(storage_dir=str(tmp_path))
    with pytest.raises(ValidationError):
        await svc.upload(session, actor=1, original_name="big.pdf",
                         content=b"%PDF" + b"0" * (50 * 1024 * 1024))


async def test_upload_text_edi_accepted(session, tmp_path):
    """텍스트형 전문(EDI/CSV) 허용."""
    svc = NoticeUploadService(storage_dir=str(tmp_path))
    staging = await svc.upload(session, actor=1, original_name="notice.txt",
                               content="접수번호|2026-001234|진료년월|2026-04\n".encode("euc-kr"))
    assert staging.status == "NEW"


# ── 이의신청 (FR-40) ────────────────────────────────────────
async def _adjustment(session, amt=109_426):
    batch = ClaimBatch(receipt_no="R-001", care_month="2026-04",
                       notice_arrived_on=TODAY - timedelta(days=10))
    session.add(batch)
    await session.flush()
    stmt = ClaimStatement(batch_id=batch.id, stmt_serial="00103",
                          chart_no="C-9", dept_code="OS", doctor_id=5,
                          claimed_amt=1_204_300, adjusted_amt=amt)
    session.add(stmt)
    await session.flush()
    adj = ClaimAdjustment(statement_id=stmt.id, adj_amt=amt, map_status="AUTO")
    session.add(adj)
    await session.flush()
    return adj


async def test_appeal_deadline_calculated(session):
    svc = AppealService()
    adj = await _adjustment(session)
    arrived = TODAY - timedelta(days=10)
    appeal = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                            notice_arrived_on=arrived, actor=1)
    from erp.claims.deadline import appeal_deadline
    assert appeal.legal_deadline == appeal_deadline(arrived)  # G5 법령 기반 (§87+§161)


async def test_appeal_past_deadline_rejected(session):
    svc = AppealService()
    adj = await _adjustment(session)
    with pytest.raises(ValidationError):
        # §161 롤링(주말·공휴일 최대 연쇄) 여유를 두고 확실히 경과한 날짜
        await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                       notice_arrived_on=TODAY - timedelta(days=100), actor=1)


async def test_rejected_appeal_no_double_loss_f2(session):
    """결정시점 인식 모델(#621 SoT) — 기각(삭감 유지)은 review 인식분과 동일하므로 추가 전표
    없음(과거 이중계상 제거). 최종 손실만 기록."""
    svc = AppealService()
    adj = await _adjustment(session, amt=109_426)
    appeal = await svc.open(session, adjustment_id=adj.id, appeal_type="REVIEW_REQUEST",
                            notice_arrived_on=TODAY - timedelta(days=5), actor=1)
    await svc.transition(session, appeal_id=appeal.id, status="SUPPLEMENT", actor=1)
    await svc.transition(session, appeal_id=appeal.id, status="REJECTED", actor=1)

    assert appeal.final_loss_amt == 109_426       # 삭감 유지(review 인식분과 동일)
    assert appeal.voucher_ref is None             # 추가 전표 없음(이중계상 아님)
    assert (await session.execute(select(Voucher))).first() is None


async def test_accepted_appeal_reverses_loss(session):
    """결정시점 인식 모델 — 인정(삭감 취소)은 review에서 인식된 삭감손실을 역분개(DR 1140/
    CR 5910)해 미수 복원 → 이후 공단 재지급이 1140 소멸."""
    svc = AppealService()
    adj = await _adjustment(session, amt=109_426)
    appeal = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                            notice_arrived_on=TODAY - timedelta(days=5), actor=1)
    await svc.transition(session, appeal_id=appeal.id, status="ACCEPTED", actor=1)

    assert appeal.final_loss_amt == 0 and appeal.voucher_ref
    voucher = (await session.execute(select(Voucher))).scalar_one()
    assert voucher.source_ref == f"appeal-adj-{appeal.id}"
    by_acc = {ln.account_code: (ln.side, float(ln.amount)) for ln in
              (await session.execute(select(VoucherLine).where(
                  VoucherLine.voucher_id == voucher.id))).scalars()}
    assert by_acc["1140"] == ("DEBIT", 109_426)   # 미수 복원
    assert by_acc["5910"] == ("CREDIT", 109_426)  # 삭감손실 환입


async def test_partial_accepted_appeal_reverses_delta(session):
    """부분 인정 — 최종 잔여 손실(final_loss_amt)만 유지, 취소분(booked−final)만 역분개."""
    svc = AppealService()
    adj = await _adjustment(session, amt=100_000)
    appeal = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                            notice_arrived_on=TODAY - timedelta(days=5), actor=1)
    await svc.transition(session, appeal_id=appeal.id, status="ACCEPTED", actor=1,
                         final_loss_amt=30_000)   # 3만만 손실 유지, 7만 취소
    assert appeal.final_loss_amt == 30_000
    voucher = (await session.execute(select(Voucher))).scalar_one()
    by_acc = {ln.account_code: (ln.side, float(ln.amount)) for ln in
              (await session.execute(select(VoucherLine).where(
                  VoucherLine.voucher_id == voucher.id))).scalars()}
    assert by_acc["1140"] == ("DEBIT", 70_000)    # 취소분만 미수 복원
    assert by_acc["5910"] == ("CREDIT", 70_000)


async def test_invalid_transition_rejected(session):
    svc = AppealService()
    adj = await _adjustment(session)
    appeal = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                            notice_arrived_on=TODAY, actor=1)
    await svc.transition(session, appeal_id=appeal.id, status="ACCEPTED", actor=1)
    with pytest.raises(ValidationError):
        await svc.transition(session, appeal_id=appeal.id, status="REJECTED", actor=1)


async def test_deadline_alert_targets(session):
    svc = AppealService()
    adj = await _adjustment(session)
    appeal = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                            notice_arrived_on=TODAY - timedelta(days=50), actor=1)
    # 알림 로직 자체를 달력과 무관하게 검증 — 기한을 D-7로 고정
    appeal.legal_deadline = TODAY + timedelta(days=7)
    await session.flush()
    targets = await svc.deadline_alert_targets(session, today=TODAY)
    assert [t.id for t in targets] == [appeal.id]
    assert appeal.alert_stage == 7                       # 발송 버킷 기록
    # 멱등 — 같은 버킷 재실행 시 재발송 없음
    assert await svc.deadline_alert_targets(session, today=TODAY) == []


async def test_deadline_alert_no_miss_on_skipped_batch(session):
    """무손실 — 배치가 하루 누락되어 D-7을 건너뛰어도 D-6에서 7-버킷 알림이 발송된다(≤ 판정)."""
    svc = AppealService()
    adj = await _adjustment(session)
    appeal = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                            notice_arrived_on=TODAY - timedelta(days=50), actor=1)
    appeal.legal_deadline = TODAY + timedelta(days=6)   # D-7 배치가 누락되어 D-6에 첫 실행
    await session.flush()
    targets = await svc.deadline_alert_targets(session, today=TODAY)
    assert [t.id for t in targets] == [appeal.id] and appeal.alert_stage == 7  # 7-버킷 발송(소실 없음)


async def test_deadline_alert_escalates_to_tighter_bucket(session):
    """버킷 상승 — 7-버킷 발송 후 D-3 진입 시 3-버킷으로 재발송(더 임박)."""
    svc = AppealService()
    adj = await _adjustment(session)
    appeal = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                            notice_arrived_on=TODAY - timedelta(days=50), actor=1)
    appeal.legal_deadline = TODAY + timedelta(days=7)
    await session.flush()
    await svc.deadline_alert_targets(session, today=TODAY)          # 7-버킷 발송
    assert appeal.alert_stage == 7
    # D-3 진입(today가 4일 경과) → 3-버킷 재발송
    later = TODAY + timedelta(days=4)                                # 기한까지 3일 남음
    targets = await svc.deadline_alert_targets(session, today=later)
    assert [t.id for t in targets] == [appeal.id] and appeal.alert_stage == 3


# ── 통계 (FR-48) ────────────────────────────────────────────
async def test_stats_by_dept_and_automation_rate(session):
    stats = ClaimStatsService()
    await _adjustment(session)
    rows = await stats.by_dimension(session, by="dept")
    assert rows[0]["dim"] == "OS" and rows[0]["amount"] == 109_426
    rate = await stats.automation_rate(session)
    assert rate == {"total": 1, "auto": 1, "rate": 100.0}


async def test_stats_dept_scope_filter(session):
    """부서장 scope — 집계에도 명시 WHERE (design §6.2)."""
    stats = ClaimStatsService()
    await _adjustment(session)
    rows = await stats.by_dimension(session, by="dept", scope_dept_codes=["IM"])
    assert rows == []  # OS 데이터는 IM scope에서 비노출


# ── 부서경계 IDOR (라우터, 심층 재검토 보완) ─────────────────────────────
def _mgr(dept_id):
    from erp.common.router import CurrentUser
    return CurrentUser(user_id=100 + dept_id, role="MANAGER", dept_id=dept_id, license_flag=True)


def _admin():
    from erp.common.router import CurrentUser
    return CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)


async def _os_dept(session):
    """stmt.dept_code='OS'와 매칭될 부서(코드 OS) + 무관 부서(IM)."""
    from erp.common.models import Department
    os_d = Department(name="정형외과", code="OS")
    im = Department(name="내과", code="IM")
    session.add_all([os_d, im])
    await session.flush()
    return os_d, im


async def test_statements_dept_boundary_idor(session):
    """명세서(chart_no PII) 조회 — 타 부서 MANAGER는 비노출, 본부서/ADMIN은 노출."""
    from erp.claims.router import list_statements
    adj = await _adjustment(session)                 # dept_code='OS'
    batch_id = (await session.get(ClaimStatement, adj.statement_id)).batch_id
    os_d, im = await _os_dept(session)
    # 타 부서(IM) MANAGER — 빈 목록(chart_no 누수 차단)
    r_other = await list_statements(batch_id=batch_id, user=_mgr(im.id), session=session)
    assert r_other["items"] == []
    # 본부서(OS) MANAGER·ADMIN — 노출
    r_own = await list_statements(batch_id=batch_id, user=_mgr(os_d.id), session=session)
    r_admin = await list_statements(batch_id=batch_id, user=_admin(), session=session)
    assert len(r_own["items"]) == 1 and r_own["items"][0]["chart_no"] == "C-9"
    assert len(r_admin["items"]) == 1


async def test_appeal_dept_boundary_idor(session):
    """이의신청 개설·목록 — 타 부서 MANAGER는 타부서 삭감건 개설 차단·목록 비노출."""
    from erp.claims.router import AppealOpen, list_appeals, open_appeal
    from erp.errors import ForbiddenError
    adj = await _adjustment(session)                 # dept_code='OS'
    os_d, im = await _os_dept(session)
    body = AppealOpen(adjustment_id=adj.id, appeal_type="OBJECTION",
                      notice_arrived_on=TODAY - timedelta(days=5))
    # 타 부서(IM) MANAGER — 개설 차단(손실전표 무단생성 방지 연쇄)
    with pytest.raises(ForbiddenError, match="타 부서"):
        await open_appeal(body=body, user=_mgr(im.id), session=session)
    # 본부서(OS) MANAGER — 개설 허용
    created = await open_appeal(body=body, user=_mgr(os_d.id), session=session)
    assert created["id"]
    # 목록 — 타 부서엔 비노출, 본부서엔 노출
    assert (await list_appeals(user=_mgr(im.id), session=session))["items"] == []
    assert len((await list_appeals(user=_mgr(os_d.id), session=session))["items"]) == 1


async def test_stats_scope_uses_dept_codes_not_pk(session):
    """부서장 통계 — dept_id(정수PK) 대신 dept_code 후보로 경계(과차단 버그 교정)."""
    from erp.claims.router import claim_stats
    await _adjustment(session)                        # dept_code='OS'
    os_d, im = await _os_dept(session)
    # 본부서(OS) MANAGER — 정수PK 매칭이던 기존엔 빈 통계였음. 이제 코드 매칭으로 노출.
    r_own = await claim_stats(by="dept", user=_mgr(os_d.id), session=session)
    assert any(x["dim"] == "OS" for x in r_own["items"])
    # 타 부서(IM) — 비노출
    r_other = await claim_stats(by="dept", user=_mgr(im.id), session=session)
    assert r_other["items"] == []
