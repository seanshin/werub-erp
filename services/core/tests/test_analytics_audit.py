"""회계 감사·이상탐지 (ADR D-7) — Benford·중복전표·비정상 분개.

설계 보완(2026-06-23 검토): ① 비정상 분개는 수동전표(manual)만·입력시각(created_at) 기준
② 중복은 적요(거래처) 있는 그룹만 — 자동전표/빈적요 오탐 차단.
"""
from datetime import date, datetime, timedelta, timezone

from erp.analytics.audit_review_service import AuditReviewService
from erp.analytics.audit_service import AuditService
from erp.finance.models import Account
from erp.finance.service import VoucherLineDTO, VoucherService

svc = AuditService()
reviews = AuditReviewService()
vs = VoucherService()
FROM, TO = date(2026, 1, 1), date(2026, 12, 31)
KST = timezone(timedelta(hours=9))


async def _accounts(session):
    session.add_all([
        Account(code="5410", name="재료비", kind="EXPENSE"),
        Account(code="2110", name="외상매입금", kind="LIABILITY"),
    ])
    await session.flush()


async def _post(session, ref, amount, *, on=date(2026, 5, 11), detail=None,
                module="manual", created_at=None):
    v = await vs.create(session, source_module=module, source_ref=ref, voucher_on=on,
                        lines=[VoucherLineDTO("5410", "DEBIT", amount, detail=detail),
                               VoucherLineDTO("2110", "CREDIT", amount)])
    if created_at is not None:
        v.created_at = created_at        # 입력시각 제어(서버 기본값 override)
        await session.flush()
    return v


# ── Benford ──────────────────────────────────────────────────────────────────
async def test_benford_distribution_and_stats(session):
    await _accounts(session)
    amounts = [123, 145, 198, 234, 267, 312, 356, 423, 512, 678, 789, 891, 912,
               1100, 1500, 1900, 2300, 3400, 4500, 6700, 8900, 11000, 19000]
    for i, a in enumerate(amounts):
        await _post(session, f"bf{i}", a)
    out = await svc.benford(session, date_from=FROM, date_to=TO)
    assert out["sample_size"] == len(amounts)
    assert len(out["digits"]) == 9
    d1 = next(d for d in out["digits"] if d["digit"] == 1)
    assert d1["expected_pct"] == 30.1            # Benford 1번 첫자리 기대빈도
    assert d1["observed"] >= 1
    assert out["verdict"] == "표본부족(N/A)"      # 표본 23<300 → 판정 보류(소표본 가드)
    assert out["note"] is not None               # 표본<300 경고


async def test_benford_empty_sample_is_na(session):
    """표본이 없는 기간은 mad=0으로 '근접 부합'(최상등급) 오판하면 안 되고 N/A여야 한다(전체검토 F)."""
    await _accounts(session)
    out = await svc.benford(session, date_from=date(2020, 1, 1), date_to=date(2020, 12, 31))
    assert out["sample_size"] == 0
    assert out["verdict"] == "표본 없음(N/A)"
    assert out["chi_square_exceeds"] is False


async def test_benford_side_filter(session):
    await _accounts(session)
    await _post(session, "s1", 100)
    od = await svc.benford(session, date_from=FROM, date_to=TO, side="DEBIT")
    oc = await svc.benford(session, date_from=FROM, date_to=TO, side="CREDIT")
    assert od["sample_size"] == 1 and oc["sample_size"] == 1 and od["side"] == "DEBIT"


# ── 중복전표 ─────────────────────────────────────────────────────────────────
async def test_duplicates_detects_cluster(session):
    await _accounts(session)
    await _post(session, "dup1", 500000, on=date(2026, 5, 10), detail="ABC상사")
    await _post(session, "dup2", 500000, on=date(2026, 5, 12), detail="ABC상사")
    await _post(session, "other", 500000, on=date(2026, 5, 12), detail="XYZ")  # 적요 다름
    out = await svc.duplicates(session, date_from=FROM, date_to=TO, window_days=7, side="DEBIT")
    assert out["cluster_count"] == 1
    c = out["clusters"][0]
    assert c["count"] == 2 and c["amount"] == 500000 and c["detail"] == "ABC상사"
    assert c["span_days"] == 2 and out["duplicate_amount"] == 500000


async def test_duplicates_respects_window(session):
    await _accounts(session)
    await _post(session, "f1", 300000, on=date(2026, 5, 1), detail="월세")
    await _post(session, "f2", 300000, on=date(2026, 6, 1), detail="월세")  # 31일 차
    out = await svc.duplicates(session, date_from=FROM, date_to=TO, window_days=7)
    assert out["cluster_count"] == 0


async def test_duplicates_require_detail(session):
    """적요 없는 동일금액은 기본(require_detail) 제외 — 본인부담 등 정상분개 오탐 차단."""
    await _accounts(session)
    await _post(session, "n1", 10000, on=date(2026, 5, 10))        # 적요 없음
    await _post(session, "n2", 10000, on=date(2026, 5, 11))        # 적요 없음
    assert (await svc.duplicates(session, date_from=FROM, date_to=TO))["cluster_count"] == 0
    # 명시적으로 끄면 탐지(차변 5410 기준 — side 미지정 시 대변 2110도 잡혀 2클러스터)
    out = await svc.duplicates(session, date_from=FROM, date_to=TO,
                               require_detail=False, side="DEBIT")
    assert out["cluster_count"] == 1


# ── 비정상 분개 ──────────────────────────────────────────────────────────────
async def test_abnormal_weekend_offhours_round(session):
    """수동전표 + 토요일 새벽 입력 + 라운드넘버 → 세 사유 동시."""
    await _accounts(session)
    sat_2am = datetime(2026, 5, 9, 2, 0, tzinfo=KST)               # 토요일 02:00
    await _post(session, "ab1", 1_000_000, on=date(2026, 5, 8), created_at=sat_2am)
    out = await svc.abnormal_journals(session, date_from=FROM, date_to=TO)
    assert out["flagged_count"] == 1
    assert set(out["items"][0]["reasons"]) == {"weekend", "offhours", "round"}


async def test_abnormal_excludes_automated_postings(session):
    """🔴 핵심 회귀 — 자동전표(야간 배치)는 주말·심야·라운드여도 미오탐."""
    await _accounts(session)
    sat_2am = datetime(2026, 5, 9, 2, 0, tzinfo=KST)
    # 동일 조건이지만 source_module=depreciation(자동) → 검사 대상 아님
    await _post(session, "auto1", 1_000_000, on=date(2026, 5, 8),
                module="depreciation", created_at=sat_2am)
    out = await svc.abnormal_journals(session, date_from=FROM, date_to=TO)
    assert out["flagged_count"] == 0
    # 명시적으로 모듈 필터 해제하면 검사됨(분석가 재량)
    wide = await svc.abnormal_journals(session, date_from=FROM, date_to=TO, source_module=None)
    assert wide["flagged_count"] == 1


async def test_abnormal_below_threshold(session):
    """승인한도 90~100% 구간 = 한도직전 분할 의심 (평일 주간 입력으로 타이밍 사유 배제)."""
    await _accounts(session)
    mon_2pm = datetime(2026, 5, 11, 14, 0, tzinfo=KST)            # 월요일 14:00(정상시각)
    await _post(session, "t1", 4_800_000, on=date(2026, 5, 11), created_at=mon_2pm)  # 한도 96%
    await _post(session, "t2", 3_200_000, on=date(2026, 5, 11), created_at=mon_2pm)  # 64%·비라운드
    out = await svc.abnormal_journals(session, date_from=FROM, date_to=TO, threshold=5_000_000)
    assert {i["amount"] for i in out["items"] if "below_threshold" in i["reasons"]} == {4_800_000}
    assert out["by_reason"]["below_threshold"] == 1
    assert out["flagged_count"] == 1                              # t2는 어떤 사유도 없음


async def test_summary_combines_checks(session):
    await _accounts(session)
    sat_2am = datetime(2026, 5, 9, 2, 0, tzinfo=KST)
    await _post(session, "x1", 1_000_000, on=date(2026, 5, 8), created_at=sat_2am)
    out = await svc.summary(session, date_from=FROM, date_to=TO)
    assert set(out.keys()) == {"range", "benford", "duplicates", "abnormal", "open_total"}
    assert out["abnormal"]["flagged_count"] >= 1
    assert "verdict" in out["benford"]
    assert out["open_total"] >= 1


# ── 검토완료 워크플로 (ADR D-7) ──────────────────────────────────────────────
async def test_findings_carry_finding_key(session):
    """후보에 안정 finding_key 부착 — 비정상=voucher:<id>, 중복=dup:<hash>."""
    await _accounts(session)
    sat_2am = datetime(2026, 5, 9, 2, 0, tzinfo=KST)
    v = await _post(session, "k1", 1_000_000, on=date(2026, 5, 8), created_at=sat_2am)
    ab = await svc.abnormal_journals(session, date_from=FROM, date_to=TO)
    assert ab["items"][0]["finding_key"] == f"voucher:{v.id}"
    assert ab["items"][0]["review"] is None        # 아직 미검토

    await _post(session, "d1", 500000, on=date(2026, 5, 10), detail="ABC")
    await _post(session, "d2", 500000, on=date(2026, 5, 12), detail="ABC")
    du = await svc.duplicates(session, date_from=FROM, date_to=TO, side="DEBIT")
    assert du["clusters"][0]["finding_key"].startswith("dup:")


async def test_review_marks_and_excludes_from_open(session):
    """검토완료 처리 시 후보에 review 부착·open_count/open_total 감소."""
    await _accounts(session)
    sat_2am = datetime(2026, 5, 9, 2, 0, tzinfo=KST)
    v = await _post(session, "r1", 1_000_000, on=date(2026, 5, 8), created_at=sat_2am)
    key = f"voucher:{v.id}"
    await reviews.upsert(session, finding_type="ABNORMAL", finding_key=key,
                         disposition="NORMAL", note="정기 비품—정상", actor=7)
    ab = await svc.abnormal_journals(session, date_from=FROM, date_to=TO)
    item = ab["items"][0]
    assert item["review"]["status"] == "REVIEWED"
    assert item["review"]["disposition"] == "NORMAL" and item["review"]["reviewed_by"] == 7
    assert ab["flagged_count"] == 1 and ab["open_count"] == 0      # 검토완료 → 미검토 0
    # unreviewed_only=True 면 목록에서 제외
    only = await svc.abnormal_journals(session, date_from=FROM, date_to=TO, unreviewed_only=True)
    assert only["items"] == [] and only["open_count"] == 0
    # 요약 open_total에도 미반영
    assert (await svc.summary(session, date_from=FROM, date_to=TO))["open_total"] == 0


async def test_review_upsert_idempotent_and_delete(session):
    """동일 finding 재처리=갱신, 삭제 시 다시 미검토."""
    await _accounts(session)
    sat_2am = datetime(2026, 5, 9, 2, 0, tzinfo=KST)
    v = await _post(session, "r2", 1_000_000, on=date(2026, 5, 8), created_at=sat_2am)
    key = f"voucher:{v.id}"
    first = await reviews.upsert(session, finding_type="ABNORMAL", finding_key=key,
                                 disposition="NORMAL", actor=1)
    second = await reviews.upsert(session, finding_type="ABNORMAL", finding_key=key,
                                  disposition="ACTION", note="역분개 요청", actor=2)
    assert first.id == second.id and second.disposition == "ACTION"   # upsert
    assert await reviews.delete(session, review_id=second.id) is True
    assert await reviews.delete(session, review_id=second.id) is False  # 이미 없음
    ab = await svc.abnormal_journals(session, date_from=FROM, date_to=TO)
    assert ab["items"][0]["review"] is None and ab["open_count"] == 1   # 미검토 복귀


async def test_review_validates_inputs(session):
    import pytest
    with pytest.raises(ValueError):
        await reviews.upsert(session, finding_type="BOGUS", finding_key="x",
                             disposition="NORMAL")
    with pytest.raises(ValueError):
        await reviews.upsert(session, finding_type="ABNORMAL", finding_key="x",
                             disposition="BOGUS")
