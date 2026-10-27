"""공단 지급 대사 (심사청구 회계 완결) — 입금 전기(DR1110/CR1140)·variance·멱등·대사 요약."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.claims.models import ClaimBatch, ClaimPayment, ClaimStatement
from erp.claims.service import ClaimPaymentService
from erp.errors import ValidationError
from erp.finance.models import Voucher, VoucherLine

svc = ClaimPaymentService()


async def _month(session, care_month="2026-04", claimed=1_000_000, decided=900_000, ref=""):
    b = ClaimBatch(receipt_no=f"R-{care_month}{ref}", care_month=care_month,
                   notice_arrived_on=date(2026, 5, 1))
    session.add(b)
    await session.flush()
    session.add(ClaimStatement(batch_id=b.id, stmt_serial="001", dept_code="IM",
                               claimed_amt=claimed, decided_amt=decided))
    await session.flush()
    return b


async def test_record_books_payment_and_clears_ar(session):
    """지급 대사 — DR 1110 현금/CR 1140 보험미수금 전기 + 결정액 대비 variance."""
    await _month(session, decided=900_000)
    pay = await svc.record(session, actor=1, care_month="2026-04", payment_ref="PAY-1",
                           paid_on=date(2026, 6, 20), paid_amt=850_000)   # 과소지급 5만
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "claim:payment:PAY-1"))
    by_acc = {ln.account_code: (ln.side, float(ln.amount)) for ln in
              (await session.execute(select(VoucherLine).where(
                  VoucherLine.voucher_id == v.id))).scalars()}
    assert by_acc["1110"] == ("DEBIT", 850_000)     # 현금 입금
    assert by_acc["1140"] == ("CREDIT", 850_000)    # 보험미수금 소멸
    assert float(pay.expected_amt) == 900_000 and float(pay.variance) == 50_000  # 과소지급 탐지


async def test_variance_is_cumulative_across_payments(session):
    """전체검토 — variance는 월 결정액 대비 누적 지급 잔여(부분지급 각 건이 단건 비교로 큰
    과소지급을 잘못 표기하지 않음·완납 후 0)."""
    await _month(session, decided=900_000)
    p1 = await svc.record(session, actor=1, care_month="2026-04", payment_ref="PAY-A",
                          paid_on=date(2026, 6, 20), paid_amt=400_000)
    assert float(p1.variance) == 500_000        # 잔여 90만−40만
    p2 = await svc.record(session, actor=1, care_month="2026-04", payment_ref="PAY-B",
                          paid_on=date(2026, 6, 25), paid_amt=500_000)
    assert float(p2.variance) == 0              # 누적 90만 완납 → 0 (단건 비교였다면 40만 오표기)


async def test_record_idempotent_on_payment_ref(session):
    """멱등 — 동일 지급명세(payment_ref) 재처리 시 전표·행 증식 없음."""
    await _month(session)
    p1 = await svc.record(session, actor=1, care_month="2026-04", payment_ref="PAY-DUP",
                          paid_on=date(2026, 6, 20), paid_amt=900_000)
    p2 = await svc.record(session, actor=1, care_month="2026-04", payment_ref="PAY-DUP",
                          paid_on=date(2026, 6, 20), paid_amt=900_000)
    assert p1.id == p2.id
    assert len((await session.scalars(select(ClaimPayment))).all()) == 1
    assert len((await session.scalars(
        select(Voucher).where(Voucher.source_ref == "claim:payment:PAY-DUP"))).all()) == 1


async def test_record_rejects_nonpositive_and_blank_ref(session):
    await _month(session)
    with pytest.raises(ValidationError, match="지급액"):
        await svc.record(session, actor=1, care_month="2026-04", payment_ref="X",
                         paid_on=date(2026, 6, 20), paid_amt=0)
    with pytest.raises(ValidationError, match="지급명세"):
        await svc.record(session, actor=1, care_month="2026-04", payment_ref="  ",
                         paid_on=date(2026, 6, 20), paid_amt=100)


async def test_record_idempotent_across_whitespace_variants(session):
    """멱등 정규화 — 앞뒤 공백만 다른 payment_ref 재전송도 동일 지급으로 처리(uq IntegrityError 아님)."""
    await _month(session)
    p1 = await svc.record(session, actor=1, care_month="2026-04", payment_ref="PAY-WS",
                          paid_on=date(2026, 6, 20), paid_amt=900_000)
    p2 = await svc.record(session, actor=1, care_month="2026-04", payment_ref="  PAY-WS ",
                          paid_on=date(2026, 6, 20), paid_amt=900_000)
    assert p1.id == p2.id
    assert len((await session.scalars(select(ClaimPayment))).all()) == 1
    assert len((await session.scalars(
        select(Voucher).where(Voucher.source_ref == "claim:payment:PAY-WS"))).all()) == 1


async def test_record_rejects_malformed_care_month(session):
    """진료월 형식 검증 — YYYY-MM 아닌 값은 조용히 미대사되지 않고 거부."""
    await _month(session)
    with pytest.raises(ValidationError, match="진료월"):
        await svc.record(session, actor=1, care_month="2026/04", payment_ref="PAY-BAD",
                         paid_on=date(2026, 6, 20), paid_amt=100)


async def test_reconciliation_summary(session):
    """대사 요약 — 청구/결정/삭감(청구−결정)/지급/미수(결정−지급)."""
    await _month(session, claimed=1_000_000, decided=900_000)
    await svc.record(session, actor=1, care_month="2026-04", payment_ref="PAY-R",
                     paid_on=date(2026, 6, 20), paid_amt=700_000)   # 부분 지급
    r = await svc.reconciliation(session, care_month="2026-04")
    assert r == {"care_month": "2026-04", "claimed": 1_000_000, "decided": 900_000,
                 "appeal_restored": 0, "expected": 900_000,   # 이의 인정 복원 반영(2026-10-09 S-B)
                 "reduced": 100_000, "paid": 700_000, "outstanding": 200_000,
                 "advance": 0, "status": "PARTIALLY_PAID"}


async def test_overpayment_splits_excess_to_advance(session):
    """과다지급 분리 전기 — 결정액 잔여 초과분은 CR 1140이 아니라 CR 2141 선수금(공단).

    초과분까지 1140에 대변 전기하면 보험미수금이 음수(대변잔액)로 오표시되던 결함 회귀 방지.
    """
    await _month(session, decided=900_000)
    await svc.record(session, actor=1, care_month="2026-04", payment_ref="PAY-OVER",
                     paid_on=date(2026, 6, 20), paid_amt=950_000)   # 결정 90만 + 5만 초과
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == "claim:payment:PAY-OVER"))
    by_acc = {ln.account_code: (ln.side, float(ln.amount)) for ln in
              (await session.execute(select(VoucherLine).where(
                  VoucherLine.voucher_id == v.id))).scalars()}
    assert by_acc["1110"] == ("DEBIT", 950_000)
    assert by_acc["1140"] == ("CREDIT", 900_000)      # AR 소멸은 결정액 잔여까지만
    assert by_acc["2141"] == ("CREDIT", 50_000)       # 초과분은 선수금(공단)
    r = await svc.reconciliation(session, care_month="2026-04")
    assert r["outstanding"] == 0 and r["advance"] == 50_000 and r["status"] == "PAID"


async def test_advance_reclassed_when_decision_arrives(session):
    """선수금 상계 — 이후 결정 도착 시 2141 잔액을 목표로 낮추는 DR 2141/CR 1140 델타 전기(멱등)."""
    await _month(session, decided=900_000)
    await svc.record(session, actor=1, care_month="2026-04", payment_ref="PAY-ADV",
                     paid_on=date(2026, 6, 20), paid_amt=950_000)   # 선수금 5만 발생
    assert round(await svc._advance_balance(session, "2026-04")) == 50_000
    # 지각 결정 도착(추가 결정 10만) → 월 결정 100만 ≥ 지급 95만 → 선수금 전액 상계
    await _month(session, claimed=100_000, decided=100_000, ref="-late")
    out = await svc.reclass_advance(session, care_month="2026-04", on=date(2026, 7, 1))
    assert out["reclassed"] is True and out["amount"] == 50_000 and out["advance"] == 0
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == "claim:advance-reclass:2026-04:v1"))
    by_acc = {ln.account_code: (ln.side, float(ln.amount)) for ln in
              (await session.execute(select(VoucherLine).where(
                  VoucherLine.voucher_id == v.id))).scalars()}
    assert by_acc["2141"] == ("DEBIT", 50_000) and by_acc["1140"] == ("CREDIT", 50_000)
    # 재호출 = 잔액=목표 → no-op(멱등)
    again = await svc.reclass_advance(session, care_month="2026-04", on=date(2026, 7, 2))
    assert again["reclassed"] is False and again["advance"] == 0


async def test_record_payment_requires_admin(session):
    """지급 대사 기록 권한 — 기관 단위 GL 현금전기라 ADMIN 전용(claims.payments). MANAGER 거부."""
    from erp.claims.router import ClaimPaymentBody, record_claim_payment
    from erp.common.router import CurrentUser
    from erp.errors import ForbiddenError
    await _month(session)
    manager = CurrentUser(user_id=2, role="MANAGER", dept_id=1, license_flag=True)
    with pytest.raises(ForbiddenError):
        await record_claim_payment(
            body=ClaimPaymentBody(care_month="2026-04", payment_ref="PAY-MGR",
                                  paid_on=date(2026, 6, 20), paid_amt=100_000),
            user=manager, session=session)


async def test_expected_none_when_no_decided_data(session):
    """결정 데이터(MEDRLT 미인제스트) 없으면 expected/variance는 None(대사 보류)·지급은 전기."""
    b = ClaimBatch(receipt_no="R-ND", care_month="2026-05", notice_arrived_on=date(2026, 6, 1))
    session.add(b)
    await session.flush()
    session.add(ClaimStatement(batch_id=b.id, stmt_serial="001", claimed_amt=500_000,
                               decided_amt=None))            # 결정액 미도착
    await session.flush()
    pay = await svc.record(session, actor=1, care_month="2026-05", payment_ref="PAY-ND",
                           paid_on=date(2026, 6, 20), paid_amt=480_000)
    assert pay.expected_amt is None and pay.variance is None
    assert float(pay.paid_amt) == 480_000               # 지급 전기는 수행


async def test_router_record_and_reconciliation(session):
    """라우터 — 지급 기록 후 대사 요약 반영(RBAC write/read)."""
    from erp.claims.router import (
        ClaimPaymentBody, claim_payment_reconciliation, record_claim_payment,
    )
    from erp.common.router import CurrentUser
    await _month(session, claimed=1_000_000, decided=900_000)
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    out = await record_claim_payment(
        body=ClaimPaymentBody(care_month="2026-04", payment_ref="PAY-RT",
                              paid_on=date(2026, 6, 20), paid_amt=900_000),
        user=admin, session=session)
    assert out["variance"] == 0 and out["voucher_ref"]
    recon = await claim_payment_reconciliation(care_month="2026-04", user=admin, session=session)
    assert recon["paid"] == 900_000 and recon["outstanding"] == 0


# ── 청구 지급 생애주기 상태 (B·마이그 0156) ──────────────────────────────
async def test_batch_status_lifecycle_decided_partial_paid(session):
    """배치 상태 — 미지급(DECIDED)→부분지급(PARTIALLY_PAID)→지급완료(PAID) 전이."""
    b = await _month(session, care_month="2026-07", claimed=1_000_000, decided=900_000)
    assert b.status == "DECIDED"                       # 통보서 인제스트 직후=미지급
    await svc.record(session, actor=1, care_month="2026-07", payment_ref="P-1",
                     paid_on=date(2026, 8, 1), paid_amt=400_000)
    assert (await session.get(ClaimBatch, b.id)).status == "PARTIALLY_PAID"
    await svc.record(session, actor=1, care_month="2026-07", payment_ref="P-2",
                     paid_on=date(2026, 8, 10), paid_amt=500_000)   # 누적 90만=결정액
    assert (await session.get(ClaimBatch, b.id)).status == "PAID"


async def test_late_batch_reverts_month_to_partial(session):
    """지급 완료된 월에 지각 통보서 유입 → 결정액↑ → 부분지급으로 복원(refresh)."""
    b1 = await _month(session, care_month="2026-09", claimed=500_000, decided=500_000)
    await svc.record(session, actor=1, care_month="2026-09", payment_ref="P-L",
                     paid_on=date(2026, 10, 1), paid_amt=500_000)
    assert (await session.get(ClaimBatch, b1.id)).status == "PAID"
    # 같은 월 지각 배치(추가 결정 30만) → 월 결정 80만 > 지급 50만 → 부분지급
    await _month(session, care_month="2026-09", claimed=300_000, decided=300_000, ref="-late")
    await svc.refresh_status(session, "2026-09")
    assert (await session.get(ClaimBatch, b1.id)).status == "PARTIALLY_PAID"


async def test_router_batches_unpaid_filter(session):
    """라우터 — 미지급(unpaid) 필터가 DECIDED/PARTIALLY_PAID만 반환, PAID 제외."""
    from erp.claims.router import list_batches
    from erp.common.router import CurrentUser
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    await _month(session, care_month="2026-11", claimed=200_000, decided=200_000)     # 미지급
    paid_b = await _month(session, care_month="2026-12", claimed=200_000, decided=200_000)
    await svc.record(session, actor=1, care_month="2026-12", payment_ref="P-F",
                     paid_on=date(2027, 1, 5), paid_amt=200_000)                       # 지급완료
    assert (await session.get(ClaimBatch, paid_b.id)).status == "PAID"
    out = await list_batches(user=admin, session=session, unpaid=True)
    months = {i["care_month"] for i in out["items"]}
    assert "2026-11" in months and "2026-12" not in months   # 미지급만


# ── 이중소스(1140 his.claim ↔ decided MEDRLT) 인식 AR 기준 (자금 갈래 회귀) ──────
async def _submission(session, *, claim_id, care_month, claimed, decided=None, decided_on=None):
    """제출 청구 캡처(ClaimSubmission) — 1140 GL 재현용 인식 AR의 소스."""
    from erp.claims.models import ClaimSubmission
    session.add(ClaimSubmission(
        claim_id=claim_id, claim_number=claim_id, care_month=care_month,
        claimed_amt=claimed, submitted_on=date(2026, 5, 1),
        decided_on=decided_on, decided_amt=decided))
    await session.flush()


async def test_interim_overpayment_before_decision_splits_to_advance(session):
    """결정 전 선지급 — MEDRLT 결정 데이터가 없어도(expected None) 제출청구 인식 AR 초과분은
    2141로 분리(음수 1140 방지). 이중소스 Finding ① 회귀."""
    # 제출 청구 100만(미결정) → 1140 인식 AR 100만. MEDRLT 통보서(ClaimStatement)는 아직 없음.
    await _submission(session, claim_id="C-INT", care_month="2026-08", claimed=1_000_000)
    pay = await svc.record(session, actor=1, care_month="2026-08", payment_ref="PAY-INT",
                           paid_on=date(2026, 6, 20), paid_amt=1_200_000)   # 인식 AR 초과 20만
    assert pay.expected_amt is None                     # MEDRLT 결정 없음(대사 보류)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "claim:payment:PAY-INT"))
    by_acc = {ln.account_code: (ln.side, float(ln.amount)) for ln in
              (await session.execute(select(VoucherLine).where(
                  VoucherLine.voucher_id == v.id))).scalars()}
    assert by_acc["1110"] == ("DEBIT", 1_200_000)
    assert by_acc["1140"] == ("CREDIT", 1_000_000)      # 인식 AR까지만 상계(음수 1140 없음)
    assert by_acc["2141"] == ("CREDIT", 200_000)        # 초과분 선수금 분리


async def test_reclass_uses_recognized_ar_not_inflated_decided(session):
    """결정액 상향(재통보) 후 reclass가 1140을 상쇄 없이 과대 대변(음수화)하지 않음 — 목표는
    MEDRLT decided가 아닌 제출청구 인식 AR 기준. 이중소스 Finding ② 회귀."""
    # 제출·심사확정 AR 50만(1140 인식). MEDRLT 통보서 결정도 50만.
    await _submission(session, claim_id="C-RE", care_month="2026-09", claimed=500_000,
                      decided=500_000, decided_on=date(2026, 6, 1))
    b = await _month(session, care_month="2026-09", claimed=500_000, decided=500_000)
    # 지급 70만 → 인식 AR 50만까지 1140, 초과 20만 2141.
    await svc.record(session, actor=1, care_month="2026-09", payment_ref="PAY-RE",
                     paid_on=date(2026, 6, 20), paid_amt=700_000)
    assert round(await svc._advance_balance(session, "2026-09")) == 200_000
    # 재통보 — MEDRLT 결정액만 50만→70만 상향. 제출청구 인식 AR(1140)은 50만 유지(his.claim 무변).
    st = await session.scalar(select(ClaimStatement).where(ClaimStatement.batch_id == b.id))
    st.decided_amt = 700_000
    await session.flush()
    out = await svc.reclass_advance(session, care_month="2026-09", on=date(2026, 7, 1))
    # 인식 AR 50만 기준 → target=max(0,70만−50만)=20만=current → no-op(1140 음수화 차단).
    assert out["reclassed"] is False
    assert round(await svc._advance_balance(session, "2026-09")) == 200_000


async def test_recognized_ar_falls_back_to_decided_without_submission(session):
    """제출청구(ClaimSubmission) 미연계면 인식 AR None → MEDRLT decided 폴백(현행 동작 보존).

    record(초과분 분리)·reclass(결정 상향 시 상계) 모두 decided 기준으로 현행대로 동작."""
    b = await _month(session, care_month="2026-10", claimed=1_000_000, decided=900_000)
    await svc.record(session, actor=1, care_month="2026-10", payment_ref="PAY-FB",
                     paid_on=date(2026, 6, 20), paid_amt=950_000)   # 결정 90만 초과 5만
    assert round(await svc._advance_balance(session, "2026-10")) == 50_000   # decided 기준 분리
    # 재통보로 결정액 상향(90만→95만) → 과다지급 해소 → reclass가 2141 상계(폴백 경로).
    st = await session.scalar(select(ClaimStatement).where(ClaimStatement.batch_id == b.id))
    st.decided_amt = 950_000
    await session.flush()
    out = await svc.reclass_advance(session, care_month="2026-10", on=date(2026, 7, 1))
    assert out["reclassed"] is True and out["amount"] == 50_000
    assert round(await svc._advance_balance(session, "2026-10")) == 0


async def test_reclass_grows_advance_on_post_payment_reduction(session):
    """지급 후 추가 삭감으로 인식 AR이 줄면 reclass가 선수금을 증액(DR1140/CR2141)해 1140 음수화
    차단 — 단방향 reclass 보완(양방향). 자금 LOW-A 회귀."""
    # 제출·확정 AR 100만. 지급 100만 → 전액 1140 상계·2141=0.
    await _submission(session, claim_id="C-BD", care_month="2026-11", claimed=1_000_000,
                      decided=1_000_000, decided_on=date(2026, 6, 1))
    b = await _month(session, care_month="2026-11", claimed=1_000_000, decided=1_000_000)
    await svc.record(session, actor=1, care_month="2026-11", payment_ref="PAY-BD",
                     paid_on=date(2026, 6, 20), paid_amt=1_000_000)
    assert round(await svc._advance_balance(session, "2026-11")) == 0
    # 재통보 추가삭감 — 인식 AR 100만→70만(승인 하향). 지급 100만은 이제 30만 과다지급.
    from erp.claims.models import ClaimSubmission
    sub = await session.scalar(select(ClaimSubmission).where(ClaimSubmission.claim_id == "C-BD"))
    sub.decided_amt = 700_000
    st = await session.scalar(select(ClaimStatement).where(ClaimStatement.batch_id == b.id))
    st.decided_amt = 700_000
    await session.flush()
    out = await svc.reclass_advance(session, care_month="2026-11", on=date(2026, 7, 1))
    assert out["reclassed"] is True and out["amount"] == -300_000       # 증액(음수 델타)
    assert round(await svc._advance_balance(session, "2026-11")) == 300_000
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == "claim:advance-reclass:2026-11:v1"))
    by_acc = {ln.account_code: (ln.side, float(ln.amount)) for ln in
              (await session.execute(select(VoucherLine).where(
                  VoucherLine.voucher_id == v.id))).scalars()}
    assert by_acc["1140"] == ("DEBIT", 300_000) and by_acc["2141"] == ("CREDIT", 300_000)
    # 이후 결정이 다시 올라오면(100만 복원) 상계로 2141 소멸 — 양방향 왕복 멱등.
    sub.decided_amt = 1_000_000
    st.decided_amt = 1_000_000
    await session.flush()
    out2 = await svc.reclass_advance(session, care_month="2026-11", on=date(2026, 7, 2))
    assert out2["reclassed"] is True and out2["amount"] == 300_000      # 감액(양수 델타)
    assert round(await svc._advance_balance(session, "2026-11")) == 0
