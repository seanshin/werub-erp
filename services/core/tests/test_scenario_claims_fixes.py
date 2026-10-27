"""2026-10-09 시나리오 통합 실행(S-A·S-B) 결함 회귀 — 재심사 정정 추론·실 UUID 키 길이·이의 동시 개시·
지급 대사 이의 복원·LIS 취소 중복 감사·산정 IDOR.

동시성 2건(이의 개시·LIS 취소)은 sqlite가 FOR UPDATE를 무시하므로 `TEST_DATABASE_URL`(PG) 레인에서만 돈다.
키 길이(VARCHAR(64))도 PG만 강제하므로 실 UUID·실 HIS ref 형태로 PG 레인에서 확인한다.
"""
import asyncio
import os
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, func, select

from erp.claims.models import (
    Appeal, ClaimAdjustment, ClaimBatch, ClaimStatement, ClaimSubmission,
)
from erp.claims.service import AppealService, ClaimPaymentService
from erp.finance.models import Voucher, VoucherLine
from erp.integrations import inbound_consumer as ic
from erp.integrations.inbound_consumer import InboundConsumer
from erp.integrations.models import HisInboundEvent
from tests._http import auth_as

consumer = InboundConsumer()
TODAY = date.today()
_PG_URL = os.environ.get("TEST_DATABASE_URL")


def _ev(domain, event_type, ref, payload):
    return HisInboundEvent(domain=domain, event_type=event_type, source_ref=ref,
                           payload=payload, status="NEW")


async def _gl(session, acct):
    d = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.account_code == acct, VoucherLine.side == "DEBIT"))
    c = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.account_code == acct, VoucherLine.side == "CREDIT"))
    return float(d) - float(c)


async def _push(session, domain, event_type, ref, payload) -> HisInboundEvent:
    session.add(_ev(domain, event_type, ref, payload))
    await session.flush()
    await consumer.consume(session)
    return await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == ref))


async def _submit(session, cid, amt):
    ev = await _push(session, "finance", "claim.submitted", f"claim:submit:{cid}", {
        "claimId": cid, "claimNumber": f"CLM-{cid[:8]}", "claimedAmount": amt,
        "careMonth": "2026-09"})
    assert ev.status == "APPLIED", ev.error


async def _review(session, cid, status, approved, reduced, **extra):
    """HIS 실 형태 — revision/correctionOf 없음·ref에 금액(insurance-claim.service.ts:200)."""
    ref = f"claim:review:{cid}:{status}:{approved}:{reduced}"
    if "revision" in extra:              # 명시 revision 재발행은 별개 이벤트(인박스 ref 구분)
        ref += f":r{extra['revision']}"
    return await _push(session, "finance", "claim.reviewed", ref, {
        "claimId": cid, "claimNumber": f"CLM-{cid[:8]}", "status": status,
        "approvedAmount": approved, "reducedAmount": reduced, "rejectReason": None, **extra})


async def _sub(session, cid):
    return await session.scalar(select(ClaimSubmission).where(ClaimSubmission.claim_id == cid))


_HOLD = "확인 후 반영"


async def _confirm(session, ev) -> HisInboundEvent:
    """2026-10-09 시나리오 5회차: 삭감 감소 재심사는 보류(DEAD) → 운영자 '확인 후 반영'(표식 동봉 재처리)."""
    assert ev.status == "DEAD" and _HOLD in (ev.error or ""), ev.error
    ev.payload = {**ev.payload, "_erpConfirmedRestore": {"by": 1, "reason": "이의 결과 대조"}}
    ev.status, ev.error, ev.retry_count = "NEW", None, 0
    await session.flush()
    await consumer.consume(session)
    await session.flush()
    await session.refresh(ev)
    return ev


# ── 1·2. claim.reviewed 재심사(revision 없음) ────────────────────────────────

async def test_rereview_full_approval_without_revision_reverses_cut(session):
    """S-B HIGH — 부분 승인(삭감 10만) 뒤 전액 승인 재심사: 종전 '삭감 없음' APPLIED로 5910 10만·1140 40만
    잔존인데 decided_amt만 50만(GL↔보조원장 불일치). 이제 정정으로 역분개·확정액 동반 이동."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    ev1 = await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    assert ev1.status == "APPLIED"
    assert await _gl(session, "5910") == 100_000
    ev2 = await _review(session, cid, "APPROVED", 500_000, 0)
    assert await _gl(session, "5910") == 100_000              # 5회차: 복원 방향은 보류(GL 불변)
    ev2 = await _confirm(session, ev2)
    assert ev2.status == "APPLIED", ev2.error
    assert await _gl(session, "5910") == 0
    assert await _gl(session, "1140") == 500_000
    sub = await _sub(session, cid)
    assert float(sub.decided_amt) == 500_000 and sub.decided_rev == 2
    # 다시 삭감(재재심사) — 역분개된 원본을 또 역분개하지 않고 새 삭감만
    ev3 = await _review(session, cid, "PARTIALLY_APPROVED", 450_000, 50_000)
    assert ev3.status == "APPLIED", ev3.error
    assert await _gl(session, "5910") == 50_000 and await _gl(session, "1140") == 450_000
    sub = await _sub(session, cid)
    assert float(sub.decided_amt) == 450_000 and sub.decided_rev == 3


async def test_rereview_changed_cut_without_revision_is_correction(session):
    """S-B MED — 30만 청구·20만/10만 → 25만/5만 재심사: 종전 '기존 전표와 금액 불일치' DEAD."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 300_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 200_000, 100_000)
    ev = await _confirm(session, await _review(session, cid, "PARTIALLY_APPROVED", 250_000, 50_000))
    assert ev.status == "APPLIED", ev.error
    assert await _gl(session, "5910") == 50_000
    assert await _gl(session, "1140") == 250_000
    sub = await _sub(session, cid)
    assert float(sub.decided_amt) == 250_000 and sub.decided_rev == 2
    refs = set((await session.scalars(select(Voucher.source_ref).where(
        Voucher.source_module == "his.claim"))).all())
    assert all(len(r) <= 64 for r in refs)      # 실 UUID — VARCHAR(64) 이내(PG 레인 강제)


async def test_rereview_same_amounts_is_idempotent(session):
    """같은 결정 재수신(다른 인박스 ref) — GL·버전 불변."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    ev = await _push(session, "finance", "claim.reviewed", f"redeliver:{cid}", {
        "claimId": cid, "status": "PARTIALLY_APPROVED", "approvedAmount": 400_000,
        "reducedAmount": 100_000})
    assert ev.status == "APPLIED", ev.error
    assert await _gl(session, "5910") == 100_000
    n = await session.scalar(select(func.count(Voucher.id)).where(
        Voucher.source_module == "his.claim", Voucher.source_ref.like("claim:review:%")))
    assert n == 1
    assert (await _sub(session, cid)).decided_rev == 1


async def test_rereview_without_amounts_goes_dead_keeping_prior(session):
    """승인·삭감액 모두 없는 재심사 — 판정 불가 → DEAD(직전 결정·GL 유지, decided_amt 지우지 않음)."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    ev = await _push(session, "finance", "claim.reviewed", f"noamt:{cid}",
                     {"claimId": cid, "status": "APPROVED"})
    assert ev.status == "DEAD" and "HIS" in (ev.error or "")
    assert await _gl(session, "5910") == 100_000
    assert float((await _sub(session, cid)).decided_amt) == 400_000


async def test_explicit_revision_still_wins(session):
    """명시 revision이 있으면 추론하지 않는다 — v1 재전송의 금액 상이는 종전대로 DEAD."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000, revision=1)
    ev = await _review(session, cid, "PARTIALLY_APPROVED", 300_000, 200_000, revision=1)
    assert ev.status == "DEAD" and "불일치" in (ev.error or "")
    ev2 = await _review(session, cid, "PARTIALLY_APPROVED", 350_000, 150_000, revision=2)
    assert ev2.status == "APPLIED"
    assert await _gl(session, "5910") == 150_000


# ── 7. 실 UUID·실 HIS ref 형태의 전표 키 길이(VARCHAR(64)) ─────────────────────

def test_bounded_ref_short_unchanged_long_hashed():
    assert ic._bounded_ref("claim:submit", "C-1") == "claim:submit:C-1"   # 기존 키 호환
    bid = str(uuid.uuid4())
    a = ic._bounded_ref(f"adjust:{bid}", f"REFUND:billing:refund:{bid}:1760000000001")
    b = ic._bounded_ref(f"adjust:{bid}", f"REFUND:billing:refund:{bid}:1760000000002")
    assert len(a) <= 64 and len(b) <= 64 and a != b
    assert a == ic._bounded_ref(f"adjust:{bid}", f"REFUND:billing:refund:{bid}:1760000000001")
    assert a.startswith(f"adjust:{bid}:#")
    long_head = ic._bounded_ref("maint:" + "X" * 80, "2026-10-09T03:12:45.123Z")
    assert len(long_head) <= 64 and long_head.startswith("maint:#")
    assert len(ic._bounded_ref("claim:review", "Y" * 80, limit=56)) <= 56


async def test_billing_adjusted_real_uuid_refs_post_and_stay_idempotent(session):
    """S-A CRITICAL — 실 HIS 환불·취소(UUID·`billing:refund:<uuid>:<ms>`) 전표 키가 102~116자 →
    PG VARCHAR(64) 절단 오류로 전량 DEAD. 부분환불 2회·취소 모두 전기, 같은 이벤트 재처리는 무중복."""
    bid = str(uuid.uuid4())
    refs = [f"billing:refund:{bid}:1760000000001", f"billing:refund:{bid}:1760000000002"]
    for i, ref in enumerate(refs):
        ev = await _push(session, "finance", "billing.adjusted", ref, {
            "billingId": bid, "type": "REFUND", "amount": 10_000 * (i + 1),
            "at": "2026-10-09T01:00:00Z"})
        assert ev.status == "APPLIED", ev.error
    cancel_ref = f"billing:cancel:{bid}"
    ev = await _push(session, "finance", "billing.adjusted", cancel_ref, {
        "billingId": bid, "type": "CANCEL", "amount": 5_000})
    assert ev.status == "APPLIED", ev.error
    rows = (await session.scalars(select(Voucher.source_ref).where(
        Voucher.source_module == "his.billing"))).all()
    assert len(rows) == 3 and len(set(rows)) == 3 and all(len(r) <= 64 for r in rows)
    # 같은 이벤트 재처리(재큐) — 같은 키 → 이중전기 없음
    await ic._billing_adjusted(session, {"billingId": bid, "type": "REFUND", "amount": 10_000},
                               "billing.adjusted", {"source_ref": refs[0]})
    assert await session.scalar(select(func.count(Voucher.id)).where(
        Voucher.source_module == "his.billing")) == 3
    assert await _gl(session, "4110") == 35_000          # 수익 차감(차변)


async def test_other_external_keys_fit_varchar64(session):
    """키 스윕 — 유지보수(긴 자산번호+ISO 시각)·제증명(UUID)·검진권(인박스 ref 최대 120자)."""
    from tests.test_inbound_consumer import _seed_voucher_rules
    _seed_voucher_rules(session)
    await session.flush()
    doc = str(uuid.uuid4())
    cases = [
        ("assets", "equipment.maintenance", f"equipment:maintenance:{uuid.uuid4()}",
         {"assetNumber": "EQ-" + str(uuid.uuid4()), "cost": 120_000,
          "performedAt": "2026-10-09T03:12:45.123Z", "maintenanceType": "PM"}),
        ("finance", "certificate.issued", f"certificate:issued:{doc}",
         {"documentId": doc, "fee": 20_000, "issuedAt": "2026-10-09"}),
        ("finance", "voucher.line_recorded",
         "voucher:purchase:" + str(uuid.uuid4()) + ":" + str(uuid.uuid4()) + ":1760000000001",
         {"lineType": "purchase", "amount": 30_000, "serial": "S-1", "currency": "KRW"}),
    ]
    for domain, et, ref, payload in cases:
        ev = await _push(session, domain, et, ref, payload)
        assert ev.status == "APPLIED", (et, ev.error)
    refs = (await session.scalars(select(Voucher.source_ref))).all()
    assert len(refs) == 3 and all(len(r) <= 64 for r in refs)


# ── 4. 지급 대사 — 이의 인정 복원 반영 ─────────────────────────────────────────

async def _month_with_adjustment(session, care_month="2026-04"):
    b = ClaimBatch(receipt_no=f"R-SB-{care_month}", care_month=care_month,
                   notice_arrived_on=TODAY - timedelta(days=10))
    session.add(b)
    await session.flush()
    st = ClaimStatement(batch_id=b.id, stmt_serial="001", chart_no="C-SB", dept_code="IM",
                        claimed_amt=1_000_000, decided_amt=900_000, adjusted_amt=100_000)
    session.add(st)
    await session.flush()
    adj = ClaimAdjustment(statement_id=st.id, adj_amt=100_000, map_status="AUTO")
    session.add(adj)
    await session.flush()
    return adj


async def test_payment_reconciliation_counts_appeal_restoration(session):
    """S-B MED — 이의 인정(10만 복원) 뒤 공단 100만 지급: GL은 맞는데 대사가 선수금 10만·차이로 표시."""
    adj = await _month_with_adjustment(session)
    svc = AppealService()
    a = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                       notice_arrived_on=TODAY - timedelta(days=5), actor=1)
    await svc.transition(session, appeal_id=a.id, status="ACCEPTED", actor=1)
    pay = await ClaimPaymentService().record(session, actor=1, care_month="2026-04",
                                             payment_ref="PAY-SB", paid_on=TODAY,
                                             paid_amt=1_000_000)
    assert float(pay.expected_amt) == 1_000_000 and float(pay.variance) == 0
    assert await _gl(session, "2141") == 0             # 복원분 재지급이 선수금으로 새지 않음
    r = await ClaimPaymentService().reconciliation(session, care_month="2026-04")
    assert r["decided"] == 900_000 and r["appeal_restored"] == 100_000
    assert r["expected"] == 1_000_000
    assert r["advance"] == 0 and r["outstanding"] == 0 and r["status"] == "PAID"


# ── 6. GET /claims/calc/from-chart 부서 경계(IDOR) ─────────────────────────────

async def test_calc_from_chart_enforces_dept_boundary(session, client):
    from erp.patient.models import BillingLine
    session.add_all([
        BillingLine(line_id="SB-L1", chart_no="C-OTHER", dept_code="OS", code="AA100",
                    treated_on=date(2026, 9, 3)),
        BillingLine(line_id="SB-L2", chart_no="C-OWN", dept_code="부서2", code="AA100",
                    treated_on=date(2026, 9, 3)),
        BillingLine(line_id="SB-L3", chart_no="C-NODEPT", dept_code=None, code="AA100",
                    treated_on=date(2026, 9, 3)),
    ])
    await session.flush()
    mgr = await auth_as(session, user_id=21, role="MANAGER", dept_id=2)
    r = await client.get("/api/v1/claims/calc/from-chart", params={"chart_no": "C-OTHER"}, headers=mgr)
    assert r.status_code == 403, r.text
    r = await client.get("/api/v1/claims/calc/from-chart", params={"chart_no": "C-NODEPT"}, headers=mgr)
    assert r.status_code == 403, r.text
    r = await client.get("/api/v1/claims/calc/from-chart",
                         params={"chart_no": "C-OWN", "care_month": "2026-09"}, headers=mgr)
    assert r.status_code == 200, r.text
    admin = await auth_as(session, user_id=22, role="ADMIN")
    r = await client.get("/api/v1/claims/calc/from-chart", params={"chart_no": "C-OTHER"}, headers=admin)
    assert r.status_code == 200, r.text


# ── 3·5. 동시성(PG 전용) ─────────────────────────────────────────────────────

pg_only = pytest.mark.skipif(not _PG_URL, reason="FOR UPDATE 동시성 — TEST_DATABASE_URL(PG) 레인 전용")


@pytest.fixture
async def pg_sm(session):   # session 의존 = conftest PG 부트스트랩(스키마·테이블) 선행 보장
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    eng = create_async_engine(_PG_URL)
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


@pg_only
async def test_concurrent_appeal_open_single_winner(pg_sm):
    """S-B HIGH — 같은 조정건에 이의 4건 동시 개시: 종전 다수 201(check-then-insert) → 5910 음수.
    조정 행 잠금으로 정확히 1건만 열린다."""
    from erp.errors import ValidationError
    async with pg_sm() as s:
        adj = await _month_with_adjustment(s, care_month="2031-01")
        adj_id, batch_receipt = adj.id, "R-SB-2031-01"
        await s.commit()

    async def one():
        async with pg_sm() as s:
            a = await AppealService().open(s, adjustment_id=adj_id, appeal_type="OBJECTION",
                                           notice_arrived_on=TODAY - timedelta(days=5), actor=1)
            await asyncio.sleep(0.3)          # 커밋 지연 — 잠금이 없으면 모두 중복검사 통과
            await s.commit()
            return a.id

    try:
        results = await asyncio.gather(*[one() for _ in range(4)], return_exceptions=True)
        won = [r for r in results if isinstance(r, int)]
        lost = [r for r in results if isinstance(r, ValidationError)]
        assert len(won) == 1 and len(lost) == 3, results
        async with pg_sm() as s:
            assert await s.scalar(select(func.count(Appeal.id)).where(
                Appeal.adjustment_id == adj_id)) == 1
    finally:
        async with pg_sm() as s:
            await s.execute(delete(Appeal).where(Appeal.adjustment_id == adj_id))
            st_id = await s.scalar(select(ClaimAdjustment.statement_id).where(
                ClaimAdjustment.id == adj_id))
            await s.execute(delete(ClaimAdjustment).where(ClaimAdjustment.id == adj_id))
            b_id = await s.scalar(select(ClaimStatement.batch_id).where(ClaimStatement.id == st_id))
            await s.execute(delete(ClaimStatement).where(ClaimStatement.id == st_id))
            await s.execute(delete(ClaimBatch).where(ClaimBatch.id == b_id,
                                                     ClaimBatch.receipt_no == batch_receipt))
            await s.commit()


@pg_only
async def test_concurrent_lis_cancel_single_audit(pg_sm):
    """S-B LOW — 같은 LIS 청구 취소 2건 동시: 종전 감사 행 2건. 행 잠금+응답 전 커밋으로 1건·나머지 already."""
    from erp.audit.models import AuditLog
    from erp.common.router import CurrentUser
    from erp.integrations.lis_webhook_router import cancel_lis_billing_claim
    from erp.integrations.models import LisBillingClaim
    lid = f"LIS-SB-{uuid.uuid4().hex[:12]}"
    async with pg_sm() as s:
        s.add(LisBillingClaim(lis_billing_id=lid, revision=1, chart_no="C-SB",
                              status="CAPTURED", item_count=1))
        await s.commit()
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)

    async def one():
        async with pg_sm() as s, s.begin():   # get_session과 같은 요청 단위 트랜잭션
            return await cancel_lis_billing_claim(lid, user=admin, session=s)

    try:
        results = await asyncio.gather(one(), one())
        assert sorted(bool(r.get("already")) for r in results) == [False, True], results
        async with pg_sm() as s:
            assert await s.scalar(select(func.count(AuditLog.id)).where(
                AuditLog.entity == "integration.lis_billing_claims",
                AuditLog.target_id == lid)) == 1
    finally:
        async with pg_sm() as s:
            await s.execute(delete(AuditLog).where(AuditLog.target_id == lid))
            await s.execute(delete(LisBillingClaim).where(LisBillingClaim.lis_billing_id == lid))
            await s.commit()


async def test_payment_ref_too_long_rejected_with_clear_message(session):
    """S-B — 51자 이상 payment_ref는 전표 키 64자를 넘겨 PG 500이었다 → 입구에서 명확한 검증 오류."""
    from erp.claims.service import PAYMENT_REF_MAX
    from erp.errors import ValidationError
    await _month_with_adjustment(session, care_month="2026-05")
    with pytest.raises(ValidationError, match="50자 이하"):
        await ClaimPaymentService().record(session, actor=1, care_month="2026-05",
                                           payment_ref="P" * (PAYMENT_REF_MAX + 1),
                                           paid_on=TODAY, paid_amt=1_000)
    ok = await ClaimPaymentService().record(session, actor=1, care_month="2026-05",
                                            payment_ref="P" * PAYMENT_REF_MAX,
                                            paid_on=TODAY, paid_amt=1_000)
    v = await session.scalar(select(Voucher.source_ref).where(
        Voucher.source_ref == f"claim:payment:{ok.payment_ref}"))
    assert v is not None and len(v) == 64


# ── 독립검토 보완(2026-10-09) — 이의 복원·버전 충돌·수동 역분개·동시 추론 ─────────────────

async def test_rereview_after_appeal_restoration_goes_dead(session):
    """HIGH — 삭감 10만 → 이의 인정(claims 모듈 DR1140/CR5910) → HIS 재심사(삭감 0·revision 없음):
    종전 추론 정정이 원본을 또 역분개해 1140 이중 복원·5910 음수. 이제 DEAD(심사팀 대조 안내)."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)                       # careMonth 2026-09
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    adj = await _month_with_adjustment(session, care_month="2026-09")
    svc = AppealService()
    a = await svc.open(session, adjustment_id=adj.id, appeal_type="OBJECTION",
                       notice_arrived_on=TODAY - timedelta(days=5), actor=1)
    await svc.transition(session, appeal_id=a.id, status="ACCEPTED", actor=1)
    assert await _gl(session, "5910") == 0
    ev = await _review(session, cid, "APPROVED", 500_000, 0)
    assert ev.status == "DEAD" and "이의신청" in (ev.error or "")
    assert await _gl(session, "5910") == 0 and await _gl(session, "1140") == 500_000
    assert float((await _sub(session, cid)).decided_amt) == 400_000


async def test_explicit_revision_colliding_with_inferred_version_is_dead(session):
    """MED — ERP 추론 v2(45만/5만) 뒤 HIS 명시 revision=2(30만/20만): 종전 무음 no-op인데 decided_amt만 30만."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    await _confirm(session, await _review(session, cid, "PARTIALLY_APPROVED", 450_000, 50_000))  # 추론 v2
    ev = await _review(session, cid, "PARTIALLY_APPROVED", 300_000, 200_000, revision=2)
    assert ev.status == "DEAD" and "불일치" in (ev.error or "")
    assert await _gl(session, "5910") == 50_000
    sub = await _sub(session, cid)
    assert float(sub.decided_amt) == 450_000 and sub.decided_rev == 2
    ok = await _review(session, cid, "PARTIALLY_APPROVED", 450_000, 50_000, revision=2)
    assert ok.status == "APPLIED", ok.error                                # 같은 금액 = 멱등


async def test_explicit_revision_colliding_with_inferred_zero_correction_is_dead(session):
    """MED — 추론 v2가 전액 승인(:rev2만 존재)인데 명시 revision=2가 삭감을 들고 옴 → DEAD."""
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    await _confirm(session, await _review(session, cid, "APPROVED", 500_000, 0))  # 추론 v2(역분개만)
    ev = await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000, revision=2)
    assert ev.status == "DEAD" and "불일치" in (ev.error or "")
    assert await _gl(session, "5910") == 0
    assert float((await _sub(session, cid)).decided_amt) == 500_000


async def test_manually_reversed_review_voucher_not_counted(session):
    """LOW — 심사 전표를 수동 역분개(`/reversal-<id>`)한 뒤 전액 승인 재심사: 종전 원본을 유효로 세어
    다시 역분개(5910 음수). 이제 순액 0 = 멱등."""
    from erp.finance.service import VoucherService
    cid = str(uuid.uuid4())
    await _submit(session, cid, 500_000)
    await _review(session, cid, "PARTIALLY_APPROVED", 400_000, 100_000)
    vid = await session.scalar(select(Voucher.id).where(
        Voucher.source_module == "his.claim", Voucher.source_ref == f"claim:review:{cid}"))
    await VoucherService().reverse(session, voucher_id=vid, reason="오전기")
    ev = await _review(session, cid, "APPROVED", 500_000, 0)
    assert ev.status == "APPLIED", ev.error
    assert await _gl(session, "5910") == 0 and await _gl(session, "1140") == 500_000


@pg_only
async def test_concurrent_inferred_rereviews_serialize(pg_sm):
    """MED — 같은 청구 재심사 2건 동시 처리(리스너+폴러): 종전 둘 다 v2 추론 → 하나 유실/충돌.
    청구 단위 advisory 잠금으로 v2·v3 순차 적용, GL 순액 = 최종 확정액과 일치."""
    cid = str(uuid.uuid4())
    async with pg_sm() as s:
        s.add(ClaimSubmission(claim_id=cid, claimed_amt=500_000, care_month="2031-02",
                              submitted_on=TODAY))
        await s.flush()
        await ic._claim_reviewed(s, {"claimId": cid, "status": "PARTIALLY_APPROVED",
                                     "approvedAmount": 400_000, "reducedAmount": 100_000},
                                 "claim.reviewed", {})
        await s.commit()

    busy = []

    async def one(approved, reduced):
        # 2026-10-09 시나리오 3회차: 잠금이 비차단(try)으로 바뀌어 패자는 기다리지 않고 ClaimLockBusy —
        # consume()에선 FAILED·재시도가 되므로 여기선 같은 재시도를 흉내 낸다(직렬화 결과는 동일해야 함).
        for _ in range(50):
            async with pg_sm() as s:
                try:
                    # 5회차: 삭감 감소 쪽은 운영자 확인 표식이 있어야 반영(순서 무관하게 둘 다 표식 동봉)
                    await ic._claim_reviewed(s, {"claimId": cid, "status": "PARTIALLY_APPROVED",
                                                 "approvedAmount": approved, "reducedAmount": reduced,
                                                 "_erpConfirmedRestore": {"by": 1, "reason": "t"}},
                                             "claim.reviewed", {})
                except ic.ClaimLockBusy:
                    busy.append(1)
                    await s.rollback()
                    await asyncio.sleep(0.1)
                    continue
                await asyncio.sleep(0.3)
                await s.commit()
                return
        raise AssertionError("잠금 재시도 소진")

    base = f"claim:review:{cid}"
    try:
        res = await asyncio.gather(one(450_000, 50_000), one(300_000, 200_000),
                                   return_exceptions=True)
        assert not [r for r in res if isinstance(r, Exception)], res
        assert busy, "동시 처리에서 잠금 경합이 관측돼야 한다(비차단)"
        async with pg_sm() as s:
            sub = await s.scalar(select(ClaimSubmission).where(ClaimSubmission.claim_id == cid))
            assert sub.decided_rev == 3
            ids = (await s.scalars(select(Voucher.id).where(
                Voucher.source_module == "his.claim",
                (Voucher.source_ref == base) | Voucher.source_ref.like(f"{base}:%")))).all()
            net = await s.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
                VoucherLine.voucher_id.in_(ids), VoucherLine.account_code == "5910",
                VoucherLine.side == "DEBIT")) - await s.scalar(
                select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
                    VoucherLine.voucher_id.in_(ids), VoucherLine.account_code == "5910",
                    VoucherLine.side == "CREDIT"))
            assert float(net) == 500_000 - float(sub.decided_amt)   # GL ↔ 보조원장 일치
    finally:
        async with pg_sm() as s:
            ids = (await s.scalars(select(Voucher.id).where(
                Voucher.source_module == "his.claim",
                (Voucher.source_ref == base) | Voucher.source_ref.like(f"{base}:%")))).all()
            await s.execute(delete(VoucherLine).where(VoucherLine.voucher_id.in_(ids)))
            await s.execute(delete(Voucher).where(Voucher.id.in_(ids)))
            await s.execute(delete(ClaimSubmission).where(ClaimSubmission.claim_id == cid))
            await s.commit()
