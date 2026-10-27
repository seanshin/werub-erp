"""변동대가 보험삭감 추정 (K-IFRS 1115) — 삭감률·미결정(제출−결정) 풀·환불부채·as-of.

미결정 풀은 제출(ClaimSubmission) 중 월말 기준 미결정(decided_on IS NULL OR > 월말). rate 이력은
결정 명세(ClaimStatement.decided_amt) 기준. 결정 신호가 실제 삭감(claim.reviewed)과 정렬돼 겹침 없음.
"""
from datetime import date

from erp.claims.models import ClaimBatch, ClaimStatement, ClaimSubmission
from erp.claims.variable_consideration_service import VariableConsiderationService
from erp.finance.models import Account

svc = VariableConsiderationService()


def _accounts():
    return [Account(code="4110", name="의료수익", kind="REVENUE"),
            Account(code="2155", name="환불부채(변동대가)", kind="LIABILITY")]


async def _rate_history(session, *, dept="IM", claimed=10_000_000, decided=9_000_000, ref="H-1"):
    """rate 이력 — 결정 명세(청구 1000만·결정 900만 → 10%)."""
    b = ClaimBatch(care_month="2026-03", receipt_no=ref, notice_arrived_on=date(2026, 4, 1),
                   decided_on=date(2026, 4, 1))
    session.add(b)
    await session.flush()
    session.add(ClaimStatement(batch_id=b.id, stmt_serial="001", dept_code=dept,
                               claimed_amt=claimed, decided_amt=decided))
    await session.flush()


async def _sub(session, *, claim_id, care_month, claimed, decided_on=None,
               submitted_on=date(2026, 5, 1)):
    """미결정 풀 — 제출 청구(ClaimSubmission)."""
    session.add(ClaimSubmission(claim_id=claim_id, care_month=care_month, claimed_amt=claimed,
                                submitted_on=submitted_on, decided_on=decided_on))
    await session.flush()


async def test_rates_and_preview(session):
    session.add_all(_accounts())
    await _rate_history(session)
    await _sub(session, claim_id="S-1", care_month="2026-06", claimed=5_000_000)   # 미결정
    rates = await svc.deduction_rates(session)
    assert rates["by_dept"]["IM"] == 0.10 and rates["overall"] == 0.10
    p = await svc.preview(session, yyyymm="2026-06")
    # 미결정 제출 500만 × 10% = 50만
    assert p["submitted_undecided"] == 5_000_000
    assert p["expected_deduction"] == 500_000 and p["delta"] == 500_000


async def test_post_idempotent(session):
    session.add_all(_accounts())
    await _rate_history(session)
    await _sub(session, claim_id="S-1", care_month="2026-06", claimed=5_000_000)
    r1 = await svc.post(session, yyyymm="2026-06")
    assert r1["posted"] and r1["delta"] == 500_000
    r2 = await svc.post(session, yyyymm="2026-06")
    assert r2["posted"] is False        # 환불부채 반영 후 차액 0(멱등)
    assert await svc._refund_balance(session, as_of=date(2026, 6, 30)) == 500_000


async def test_multimonth_does_not_reverse_prior_reserve(session):
    """다월 미결정 — 차월 마감이 전월 적립분을 환입하지 않아야 한다(누적 환불부채)."""
    session.add_all(_accounts())
    await _rate_history(session)
    await _sub(session, claim_id="M", care_month="2026-05", claimed=5_000_000)
    await _sub(session, claim_id="J", care_month="2026-06", claimed=1_000_000)
    r5 = await svc.post(session, yyyymm="2026-05")   # ≤5월: 500만×10% = 50만
    assert r5["delta"] == 500_000
    r6 = await svc.post(session, yyyymm="2026-06")   # ≤6월: 600만×10% = 60만 → +10만(환입 아님)
    assert r6["delta"] == 100_000
    assert await svc._refund_balance(session, as_of=date(2026, 6, 30)) == 600_000


async def test_decided_submission_excluded_asof_no_double_count(session):
    """겹침 제거·as-of — decided_on 마킹된 제출은 그 이후 as-of에서 풀 제외(실적 5910과 이중차감 방지)."""
    session.add_all(_accounts())
    await _rate_history(session)
    # 6월 제출 500만, 7/5 결정(claim.reviewed가 decided_on 마킹)
    await _sub(session, claim_id="D-1", care_month="2026-06", claimed=5_000_000,
               decided_on=date(2026, 7, 5))
    # as-of 6월말: 결정일(7/5) > 6/30 → 미결정 → 풀 포함
    p_jun = await svc.preview(session, yyyymm="2026-06")
    assert p_jun["submitted_undecided"] == 5_000_000 and p_jun["expected_deduction"] == 500_000
    # as-of 7월말: 결정일(7/5) ≤ 7/31 → 결정 → 풀 제외(그새 실제 삭감 5910 계상돼도 이중차감 없음)
    p_jul = await svc.preview(session, yyyymm="2026-07")
    assert p_jul["submitted_undecided"] == 0 and p_jul["expected_deduction"] == 0


async def test_unattributed_submission_excluded(session):
    """진료월 미동봉(care_month NULL) 제출은 월 귀속 불가라 풀에서 제외."""
    session.add_all(_accounts())
    await _rate_history(session)
    await _sub(session, claim_id="N-1", care_month=None, claimed=9_000_000)   # 미동봉
    await _sub(session, claim_id="N-2", care_month="2026-06", claimed=1_000_000)
    p = await svc.preview(session, yyyymm="2026-06")
    assert p["submitted_undecided"] == 1_000_000       # 미동봉 900만 제외


async def test_post_true_up_on_growing_pool(session):
    """증분 재전기 — 같은 월 풀이 커지면 재-post가 새 증분을 실제로 전기해야 한다.

    월 고정 source_ref였을 때 VoucherService (module,ref) 멱등에 걸려 2번째 증분이 전기 안 되면서
    posted:True만 반환하던 결함(#627 동형) 회귀 방지. posting별 vN 유니크화로 각 증분이 자기 전표를 남긴다.
    """
    session.add_all(_accounts())
    await _rate_history(session)
    await _sub(session, claim_id="S-1", care_month="2026-06", claimed=5_000_000,
               submitted_on=date(2026, 6, 15))
    r1 = await svc.post(session, yyyymm="2026-06")   # 500만×10% = 50만
    assert r1["posted"] and r1["delta"] == 500_000
    # 6월 제출이 더 도착(월중 연속 캡처) — 풀 800만으로 증가
    await _sub(session, claim_id="S-2", care_month="2026-06", claimed=3_000_000,
               submitted_on=date(2026, 6, 20))
    r2 = await svc.post(session, yyyymm="2026-06")   # 800만×10% = 80만, 잔액 50만 → +30만
    assert r2["posted"] and r2["delta"] == 300_000
    # 핵심: 증분이 실제 전기돼 환불부채가 80만이어야 한다(월 고정키였으면 50만서 멈춤)
    assert await svc._refund_balance(session, as_of=date(2026, 6, 30)) == 800_000
    # 변화 없이 다시 = 증분 0 no-op(멱등 유지)
    r3 = await svc.post(session, yyyymm="2026-06")
    assert r3["posted"] is False


async def test_preview_excludes_future_submission_asof(session):
    """as-of 제출 가드 — 월말 후 접수된 제출은 그 월 as-of 풀에서 제외(과거월 재실행 미래누출 방지)."""
    session.add_all(_accounts())
    await _rate_history(session)
    # care_month은 6월이나 실제 제출은 7/10(월말 후) — 6월 as-of 풀에서 빠져야
    await _sub(session, claim_id="LATE", care_month="2026-06", claimed=5_000_000,
               submitted_on=date(2026, 7, 10))
    p_jun = await svc.preview(session, yyyymm="2026-06")
    assert p_jun["submitted_undecided"] == 0 and p_jun["expected_deduction"] == 0
    # 7월 as-of에는 제출(7/10 ≤ 7/31)됐고 미결정이라 포함
    p_jul = await svc.preview(session, yyyymm="2026-07")
    assert p_jul["submitted_undecided"] == 5_000_000


async def test_deduction_rate_clamped_non_negative(session):
    """삭감률 클램프 — 결정액>청구액(가산/이자) 음수율은 [0,1]로 클램프(부채 음수화 방지)."""
    session.add_all(_accounts())
    b = ClaimBatch(care_month="2026-03", receipt_no="NEG-1", notice_arrived_on=date(2026, 4, 1),
                   decided_on=date(2026, 4, 1))
    session.add(b)
    await session.flush()
    session.add(ClaimStatement(batch_id=b.id, stmt_serial="001", dept_code="GS",
                               claimed_amt=1_000_000, decided_amt=1_200_000))   # 결정>청구
    await _sub(session, claim_id="G-1", care_month="2026-06", claimed=3_000_000)
    rates = await svc.deduction_rates(session)
    assert rates["overall"] == 0.0                     # 음수율 → 0 클램프
    p = await svc.preview(session, yyyymm="2026-06")
    assert p["expected_deduction"] == 0 and p["delta"] == 0
