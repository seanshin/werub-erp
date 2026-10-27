"""보험심사 워크리스트 (PR-A) — Tier1 룰·케이스 멱등/해소·상태전이·결정."""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from erp.claims.pre_review import PreReviewEngine
from erp.claims.worklist_service import WorklistService
from erp.errors import ValidationError

svc = WorklistService()


def test_classify_clinical_areas():
    """임상영역 분류 — 명칭 키워드로 처치·수술/검사/영상/주사/투약 분리(심사 스캔)."""
    from erp.patient.billing_classify import classify_clinical
    assert classify_clinical("TARIFF", "체외금속고정술(전완골)") == "PROC"
    assert classify_clinical("TARIFF", "약물 및 독물-[정밀면역검사](정량)") == "LAB"
    assert classify_clinical("TARIFF", "흉부 단순방사선촬영") == "IMAGING"
    assert classify_clinical("TARIFF", "정맥내 주사") == "INJECT"
    assert classify_clinical("TARIFF", "전신마취") == "ANES"
    assert classify_clinical("TARIFF", "재진진찰료") == "CONSULT"
    assert classify_clinical("DRUG", "아목시실린정") == "DRUG"
    assert classify_clinical("MATERIAL", "수술용 봉합사") == "MATERIAL"
    # 검사가 처치보다 우선(정밀면역검사는 '검사', 고정술은 '처치')
    assert classify_clinical("TARIFF", "내시경적 점막절제술") == "PROC"


def _rule(rule_type, params=None, severity="WARN", code=None):
    return SimpleNamespace(code=code or f"R-{rule_type}", name=rule_type,
                           rule_type=rule_type, params=params or {},
                           severity=severity, active=True)


def _ln(chart="C1", code="AA100", count=1, amount=None, treated_on=date(2026, 6, 1), **kw):
    return {"chart_no": chart, "code": code, "count": count, "amount": amount,
            "treated_on": treated_on, **kw}


# ── Tier 1 evaluator ──────────────────────────────────────
def test_age_sex_limit():
    rules = [_rule("AGE_SEX_LIMIT", {"code": "AA100", "min_age": 18, "sex": "F"},
                   severity="BLOCK")]
    lines = [_ln(age=10, sex="M"),                    # 연령 미달 + 성별 위반 → 2건
             _ln(chart="C2", age=30, sex="F"),        # 적합
             _ln(chart="C3", code="BB200", age=5)]    # 대상 코드 아님
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    assert len(out) == 2 and all(f.chart_no == "C1" for f in out)


def test_exclusive_pair_same_day_only():
    rules = [_rule("EXCLUSIVE_PAIR", {"codes": ["AA100", "BB200"]}, severity="BLOCK")]
    lines = [_ln(), _ln(code="BB200"),                              # 같은 날 → 적발
             _ln(chart="C2"), _ln(chart="C2", code="BB200", treated_on=date(2026, 6, 2))]
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    assert len(out) == 1 and out[0].chart_no == "C1"


def test_period_limit_monthly_window():
    rules = [_rule("PERIOD_LIMIT", {"max": 2, "window": "M"})]
    lines = [_ln(treated_on=date(2026, 6, d)) for d in (1, 10, 20)] \
        + [_ln(treated_on=date(2026, 7, 1))]                        # 다음달은 별도 집계
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    assert len(out) == 1 and "월 산정 3회" in out[0].detail


def test_surcharge_check_daytime_flagged():
    rules = [_rule("SURCHARGE_CHECK", {"surcharge_suffix": "010",
                                       "night_start": 20, "night_end": 8})]
    lines = [_ln(code="AA010", treated_at=datetime(2026, 6, 1, 14, 0)),   # 월요일 14시 → 적발
             _ln(chart="C2", code="AA010", treated_at=datetime(2026, 6, 1, 22, 0)),  # 야간 OK
             _ln(chart="C3", code="AA010", treated_at=datetime(2026, 6, 7, 14, 0)),  # 일요일 OK
             _ln(chart="C5", code="AA010", treated_at=datetime(2026, 1, 1, 14, 0)),  # 신정(고정공휴) OK
             _ln(chart="C6", code="AA010", treated_at=datetime(2026, 2, 17, 14, 0)), # 설날(연도별) OK
             _ln(chart="C7", code="AA010", treated_at=datetime(2026, 6, 13, 14, 0)), # 평일 토요일 → 적발(토요가산 별도)
             _ln(chart="C4", code="AA010")]                               # 시각 없음 → 보류
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    assert {f.chart_no for f in out} == {"C1", "C7"}   # P1-11 — 법정공휴일 주간 가산 오탐 해소(토요일은 적발 유지)


def test_surcharge_saturday_optin_not_flagged():
    """토요가산 옵트인(정책) — saturday_surcharge=True면 토요일 주간 가산코드는 정당(미적발).

    평일 주간(C1)은 여전히 적발·야간/일요일은 무관. 기본 OFF는 현행(토요일 적발) 유지.
    """
    rules = [_rule("SURCHARGE_CHECK", {"surcharge_suffix": "010", "night_start": 20,
                                       "night_end": 8, "saturday_surcharge": True})]
    lines = [_ln(code="AA010", treated_at=datetime(2026, 6, 1, 14, 0)),            # 월 14시 → 적발
             _ln(chart="C7", code="AA010", treated_at=datetime(2026, 6, 13, 14, 0)),  # 토 14시 → 정당(옵트인)
             _ln(chart="C8", code="AA010", treated_at=datetime(2026, 6, 13, 22, 0))]  # 토 야간 → 정당
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    assert {f.chart_no for f in out} == {"C1"}       # 토요일(C7·C8) 옵트인으로 미적발, 평일 주간만 적발
    # 옵트인 detail에 '토요' 노출(가산 종류 안내)
    assert "토요" in out[0].detail


def test_los_ratio_daily_volume():
    rules = [_rule("LOS_RATIO", {"per_day_max": 2})]
    lines = [_ln(code=f"X{i}") for i in range(3)]
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    assert len(out) == 1 and "행위량 3건" in out[0].detail


# ── 케이스 파이프라인 ─────────────────────────────────────
def _findings(lines, *, dup=True):
    rules = [_rule("DUPLICATE", severity="BLOCK", code="R-DUP")] if dup else []
    return PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})


async def test_save_run_idempotent_and_resolve(session):
    dup_lines = [_ln(amount=10_000), _ln(amount=10_000)]     # 중복 → 적발 1
    out1 = await svc.save_run(session, actor=1, lines=dup_lines,
                              findings=_findings(dup_lines))
    assert len(out1["cases"]) == 1
    case_id = out1["cases"][0]["case_id"]
    assert out1["cases"][0]["findings"] == 1

    # 동일 재실행 — 케이스·적발 증식 없음 (멱등)
    out2 = await svc.save_run(session, actor=1, lines=dup_lines,
                              findings=_findings(dup_lines))
    assert out2["cases"][0]["case_id"] == case_id
    detail = await svc.detail(session, case_id=case_id)
    assert len(detail["findings"]) == 1 and detail["risk_amount"] == 20_000

    # 수정요청 → 중복 제거 후 재실행 → 해소·자동 RESOLVED
    await svc.update_case(session, actor=1, case_id=case_id, status="FIX_REQUESTED")
    fixed = [_ln(amount=10_000)]
    await svc.save_run(session, actor=1, lines=fixed, findings=_findings(fixed))
    detail = await svc.detail(session, case_id=case_id)
    assert detail["status"] == "RESOLVED" and detail["finding_count"] == 0
    assert detail["findings"][0]["resolved"] is True

    # 종결 후 새 적발 → OPEN 재개
    await svc.save_run(session, actor=1, lines=dup_lines, findings=_findings(dup_lines))
    assert (await svc.detail(session, case_id=case_id))["status"] == "OPEN"


async def test_auto_actions_view(session):
    """자동 처리 근거·로그 뷰 — 자동 이상없음 케이스 + 자동 오탐 적발(근거·시각)."""
    from erp.claims.models import PreReviewCase, PreReviewFinding
    c1 = PreReviewCase(chart_no="C-AUTO1", care_month="2026-06", status="CLEARED",
                       auto_cleared=True)
    c2 = PreReviewCase(chart_no="C-AUTO2", care_month="2026-06", status="OPEN")
    session.add_all([c1, c2])
    await session.flush()
    session.add(PreReviewFinding(case_id=c2.id, rule_code="R-DUP", rule_name="중복청구",
                                 code="ZZ9", severity="WARN", decision="DISMISS",
                                 auto_decided=True, decision_note="자동 오탐(캘리브레이션: …)"))
    await session.flush()
    out = await svc.auto_actions(session)
    assert {x["chart_no"] for x in out["auto_cleared"]} == {"C-AUTO1"}   # 자동표시 케이스만
    assert len(out["auto_dismissed"]) == 1 and out["auto_dismissed"][0]["code"] == "ZZ9"
    assert "캘리브레이션" in (out["auto_dismissed"][0]["note"] or "")   # 근거 동봉


async def test_targeted_sync_chart_scope(session):
    """이벤트 기반 표적 sync — chart_no 지정 시 그 차트×월만 적재(다른 차트 미생성)."""
    from datetime import date as _date

    from sqlalchemy import select

    from erp.claims.models import PreReviewCase, PreReviewRule
    from erp.patient.models import BillingLine
    # 두 차트, 같은 월 — 각 차트에 동일코드 중복(DUPLICATE 적발 유도)
    for ch in ("C-TA", "C-TB"):
        session.add_all([
            BillingLine(line_id=f"{ch}-1", chart_no=ch, code="ZZ1", code_type="TARIFF",
                        count=1, coverage="NHIS", treated_on=_date(2026, 6, 3), unit_price_his=1000),
            BillingLine(line_id=f"{ch}-2", chart_no=ch, code="ZZ1", code_type="TARIFF",
                        count=1, coverage="NHIS", treated_on=_date(2026, 6, 3), unit_price_his=1000),
        ])
    session.add(PreReviewRule(rule_type="DUPLICATE", code="R-DUP", name="중복청구",
                              severity="WARN", active=True))
    await session.flush()
    # C-TA만 표적 sync
    out = await svc.sync_from_billing_lines(session, actor=0, care_month="2026-06", chart_no="C-TA")
    charts = {c.chart_no for c in (await session.scalars(select(PreReviewCase))).all()}
    assert "C-TA" in charts and "C-TB" not in charts     # 표적 차트만 케이스 생성
    assert out["lines"] == 2


async def test_billing_line_emits_prereview_target(session):
    """_billing_line — treated_on 있으면 (차트, 월) 표적 힌트 방출(소비→이벤트 재평가)."""
    from erp.integrations.inbound_consumer import _billing_line
    res = await _billing_line(session, {
        "lineId": "PL-1", "chartNo": "C-PR", "code": "AA100",
        "treatedOn": "2026-06-11", "count": 1}, "billing.line_recorded", {})
    assert res["_prereview"] == ("C-PR", "2026-06")     # consume이 이 키로 표적 수집
    res2 = await _billing_line(session, {                # treated_on 없으면 표적 없음
        "lineId": "PL-2", "chartNo": "C-PR", "code": "AA100", "count": 1},
        "billing.line_recorded", {})
    assert "_prereview" not in res2


async def test_p2_auto_dismiss_confirmed_false_positive(session, monkeypatch):
    """P2 자동 오탐 — (룰×코드) 인간 전건 오탐 표본 충족 시 새 적발 자동 DISMISS.

    안전: ACCEPT 1건이라도 있으면 제외, 자동결정은 집계 제외(피드백 루프 차단), 옵트인.
    """
    from erp.claims.models import PreReviewCase, PreReviewFinding
    from erp.config import get_settings

    # 옵트인 + 표본 3으로 낮춰 테스트
    s = get_settings()
    monkeypatch.setattr(s, "prereview_auto_dismiss", True, raising=False)
    monkeypatch.setattr(s, "prereview_auto_dismiss_min_sample", 3, raising=False)

    # 과거 인간 결정: (DUPLICATE, AA900) 전건 오탐 3건 시드
    c = PreReviewCase(chart_no="C-HIST", care_month="2026-05", status="CLEARED")
    session.add(c)
    await session.flush()
    for _ in range(3):
        session.add(PreReviewFinding(case_id=c.id, rule_code="R-DUP", code="AA900",
                                     severity="WARN", decision="DISMISS", decided_by=9))
    await session.flush()
    pats = await svc._auto_dismiss_patterns(session, min_sample=3)
    assert ("R-DUP", "AA900") in pats

    # 새 적발이 그 패턴이면 자동 DISMISS(가역·자동표시)
    lines = [_ln(chart="C-NEW", code="AA900", amount=5_000),
             _ln(chart="C-NEW", code="AA900", amount=5_000)]
    out = await svc.save_run(session, actor=1, lines=lines, findings=_findings(lines))
    assert out["auto_dismissed"] == 1
    d = await svc.detail(session, case_id=out["cases"][0]["case_id"])
    f0 = d["findings"][0]
    assert f0["decision"] == "DISMISS" and f0["auto_decided"] is True
    assert d["finding_count"] == 0          # 자동 오탐 → live 집계 제외


async def test_p2_auto_dismiss_disqualified_by_one_accept(session):
    """P2 보수성 — ACCEPT 1건이라도 있으면 자동 오탐 패턴에서 제외(실 삭감 가능성)."""
    from erp.claims.models import PreReviewCase, PreReviewFinding
    c = PreReviewCase(chart_no="C-MIX", care_month="2026-05", status="CLEARED")
    session.add(c)
    await session.flush()
    for dec in ("DISMISS", "DISMISS", "DISMISS", "ACCEPT"):   # 1건 인정 → 실 삭감 가능
        session.add(PreReviewFinding(case_id=c.id, rule_code="R-DUP", code="BB800",
                                     severity="WARN", decision=dec, decided_by=9))
    await session.flush()
    pats = await svc._auto_dismiss_patterns(session, min_sample=3)
    assert ("R-DUP", "BB800") not in pats


async def test_p1_auto_clear_on_resync_clean(session):
    """P1 자동 심사 — OPEN 케이스가 재-sync로 무적발(리스크0) 되면 자동 이상없음(CLEARED), 가역.

    안전경계: 적발 있으면 절대 자동처리 안 함(휴먼). 자동/수동 구분(auto_cleared)·감사·되돌리기.
    """
    from erp.claims.models import PreReviewCase
    dup = [_ln(chart="C-AC", amount=5_000), _ln(chart="C-AC", amount=5_000)]   # 중복 → 적발
    out = await svc.save_run(session, actor=1, lines=dup, findings=_findings(dup))
    cid = out["cases"][0]["case_id"]
    case = await session.get(PreReviewCase, cid)
    assert case.status == "OPEN" and case.finding_count > 0     # 적발 있음 → 자동처리 금지
    assert case.auto_cleared is False
    # 재-sync: 코드 정정 등으로 적발 사라짐 → 무적발 → 자동 CLEARED
    clean = [_ln(chart="C-AC", amount=5_000)]                   # 단일 라인 = 중복 아님
    out2 = await svc.save_run(session, actor=1, lines=clean, findings=_findings(clean))
    case = await session.get(PreReviewCase, cid)
    assert case.status == "CLEARED" and case.auto_cleared is True and out2["auto_cleared"] == 1
    # 가역 — 다시 적발 발생 시 OPEN 재개(자동표시 해제)
    await svc.save_run(session, actor=1, lines=dup, findings=_findings(dup))
    case = await session.get(PreReviewCase, cid)
    assert case.status == "OPEN" and case.auto_cleared is False


# ── 안전경계: 자동심사 피드백 루프 차단 · 킬스위치 ──────────────────

async def test_p2_pattern_excludes_auto_decided(session):
    """P2 자동→자동 피드백 루프 차단 — auto_decided(자동 DISMISS) 적발은 패턴 집계서 제외.

    자동결정이 자동결정을 강화하면 단발 오탐이 영구 패턴으로 굳어 환수 리스크 증식.
    인간 결정만 표본으로 집계해야 안전(_auto_dismiss_patterns의 auto_decided.is_(False))."""
    from erp.claims.models import PreReviewCase, PreReviewFinding
    c = PreReviewCase(chart_no="C-FB", care_month="2026-05", status="CLEARED")
    session.add(c)
    await session.flush()
    # 전부 자동결정 DISMISS 5건 — min_sample(3) 넘어도 인간표본 0이라 패턴 미형성
    for _ in range(5):
        session.add(PreReviewFinding(case_id=c.id, rule_code="R-DUP", code="CC700",
                                     severity="WARN", decision="DISMISS",
                                     auto_decided=True, decided_by=0))
    await session.flush()
    pats = await svc._auto_dismiss_patterns(session, min_sample=3)
    assert ("R-DUP", "CC700") not in pats          # 자동결정만으론 패턴 형성 불가


async def test_p2_pattern_counts_humans_only(session):
    """P2 표본은 인간 결정만 — 인간 2건(<3) + 자동 3건 = 합5건이어도 패턴 미형성.

    합산(5)이 min_sample(3)을 넘어도 인간(2)이 미달이면 자동 DISMISS 대상 아님."""
    from erp.claims.models import PreReviewCase, PreReviewFinding
    c = PreReviewCase(chart_no="C-HC", care_month="2026-05", status="CLEARED")
    session.add(c)
    await session.flush()
    for _ in range(2):                              # 인간 DISMISS 2건 (< min_sample 3)
        session.add(PreReviewFinding(case_id=c.id, rule_code="R-DUP", code="DD600",
                                     severity="WARN", decision="DISMISS", decided_by=9))
    for _ in range(3):                              # 자동 DISMISS 3건 — 집계 제외
        session.add(PreReviewFinding(case_id=c.id, rule_code="R-DUP", code="DD600",
                                     severity="WARN", decision="DISMISS",
                                     auto_decided=True, decided_by=0))
    await session.flush()
    pats = await svc._auto_dismiss_patterns(session, min_sample=3)
    assert ("R-DUP", "DD600") not in pats          # 인간 2건만 유효 → 미달


async def test_p1_auto_clear_killswitch_off(session, monkeypatch):
    """P1 킬스위치 — prereview_auto_clear=False면 무적발 재-sync에도 자동 CLEARED 안 함.

    적발이 해소돼도 상태는 수동 유지(OPEN) — 운영자가 자동 종결을 끈 의사 존중."""
    from erp.claims.models import PreReviewCase
    from erp.config import get_settings
    dup = [_ln(chart="C-KS", amount=5_000), _ln(chart="C-KS", amount=5_000)]
    out = await svc.save_run(session, actor=1, lines=dup, findings=_findings(dup))
    cid = out["cases"][0]["case_id"]
    monkeypatch.setattr(get_settings(), "prereview_auto_clear", False, raising=False)
    clean = [_ln(chart="C-KS", amount=5_000)]      # 단일 라인 = 중복 아님 → 무적발
    out2 = await svc.save_run(session, actor=1, lines=clean, findings=_findings(clean))
    case = await session.get(PreReviewCase, cid)
    assert out2["auto_cleared"] == 0
    assert case.finding_count == 0                 # 적발은 해소
    assert case.status == "OPEN" and case.auto_cleared is False   # 그러나 자동 종결 안 함


async def test_p2_auto_dismiss_off_by_default(session):
    """P2 기본 OFF(옵트인) — 확정 오탐 패턴이 있어도 prereview_auto_dismiss 미설정 시 미적용.

    기본값 False라 _auto_dismiss_patterns 자체를 호출 안 함 → 환수 리스크 기본 차단."""
    from erp.claims.models import PreReviewCase, PreReviewFinding
    # 인간 전건 오탐 패턴 시드(기본 min_sample 10건) — 옵트인 안 했으므로 무시돼야
    c = PreReviewCase(chart_no="C-OFF", care_month="2026-05", status="CLEARED")
    session.add(c)
    await session.flush()
    for _ in range(10):
        session.add(PreReviewFinding(case_id=c.id, rule_code="R-DUP", code="EE500",
                                     severity="WARN", decision="DISMISS", decided_by=9))
    await session.flush()
    lines = [_ln(chart="C-OFF2", code="EE500", amount=5_000),
             _ln(chart="C-OFF2", code="EE500", amount=5_000)]
    out = await svc.save_run(session, actor=1, lines=lines, findings=_findings(lines))
    assert out["auto_dismissed"] == 0              # 옵트인 안 함 → 자동 DISMISS 없음
    d = await svc.detail(session, case_id=out["cases"][0]["case_id"])
    assert d["findings"][0]["auto_decided"] is False and d["finding_count"] > 0


async def test_transition_rules_and_assign(session):
    lines = [_ln(amount=5_000), _ln(amount=5_000)]
    out = await svc.save_run(session, actor=1, lines=lines, findings=_findings(lines))
    cid = out["cases"][0]["case_id"]

    with pytest.raises(ValidationError, match="전이"):
        await svc.update_case(session, actor=1, case_id=cid, status="RESOLVED")  # OPEN→RESOLVED 불가
    c = await svc.update_case(session, actor=7, case_id=cid, status="IN_REVIEW")
    assert c.assignee_id == 7                                  # 미배정 → 검토자 자동 배정


async def test_manual_close_blocked_by_unresolved_block_finding(session):
    """안전경계(수동·자동 대칭) — 미해소 BLOCK 적발이 있는 케이스는 수동/일괄 종결 불가.

    P1 자동경로는 finding_count==0에서만 CLEARED하는데(불변식), 수동경로(update_case/bulk_status)는
    전이규칙만 검사해 BLOCK을 조용히 종결하던 비대칭을 차단. 오탐 처리(해소) 후에는 종결 허용(과차단 아님).
    """
    dup = [_ln(chart="C-BLK", amount=5_000), _ln(chart="C-BLK", amount=5_000)]   # 중복 → BLOCK 적발
    out = await svc.save_run(session, actor=1, lines=dup, findings=_findings(dup))
    cid = out["cases"][0]["case_id"]
    d = await svc.detail(session, case_id=cid)
    assert d["finding_count"] == 1 and d["max_severity"] == "BLOCK"

    # 수동 종결 차단 — CLEARED/RESOLVED 모두(전이 자체는 허용이나 BLOCK 미해소로 게이트)
    with pytest.raises(ValidationError, match="BLOCK"):
        await svc.update_case(session, actor=1, case_id=cid, status="CLEARED")

    # 일괄 종결도 조용히 넘어가지 않고 skipped로 사유 표면화(전체 배치 실패는 아님)
    res = await svc.bulk_status(session, actor=1,
                                items=[{"chart_no": "C-BLK", "care_month": "2026-06",
                                        "status": "CLEARED"}])
    assert res["applied"] == 0 and len(res["skipped"]) == 1
    assert "BLOCK" in res["skipped"][0]["reason"]

    # 오탐 처리로 적발 해소 → finding_count 0 → 종결 허용
    fid = d["findings"][0]["id"]
    await svc.decide_finding(session, actor=1, finding_id=fid,
                             decision="DISMISS", note="동일코드 정당 분할")
    c = await svc.update_case(session, actor=1, case_id=cid, status="CLEARED")
    assert c.status == "CLEARED"


async def test_manual_close_allows_warn_only_case(session):
    """과차단 방지 — BLOCK이 아닌 WARN-only 케이스는 수동 종결 허용(심사자 판단 존중)."""
    warn_rule = [_rule("PERIOD_LIMIT", {"max": 2, "window": "M"})]               # 기본 severity=WARN
    lines = [_ln(chart="C-WARN", treated_on=date(2026, 6, d), amount=1_000) for d in (1, 10, 20)]
    findings = PreReviewEngine().evaluate(lines=lines, rules=warn_rule, fee={})
    out = await svc.save_run(session, actor=1, lines=lines, findings=findings)
    cid = out["cases"][0]["case_id"]
    d = await svc.detail(session, case_id=cid)
    assert d["finding_count"] >= 1 and d["max_severity"] == "WARN"
    c = await svc.update_case(session, actor=1, case_id=cid, status="CLEARED")    # WARN → 종결 허용
    assert c.status == "CLEARED"


async def test_gated_only_case_not_auto_cleared_with_both_flags_on(session, monkeypatch):
    """방어심층 — auto_dismiss+auto_clear 동시 ON이어도 physician_gated-only 케이스는 자동 CLEARED 안 됨.

    gated 룰은 패턴에서 제외(의료법·#315)되어 자동 DISMISS되지 않으므로 적발이 live로 남고,
    자동 CLEARED(finding_count==0 조건)가 발화하지 않는다(의료진 확인 없이 자동 종결 금지).
    """
    from erp.claims.models import PreReviewCase, PreReviewFinding, PreReviewRule
    from erp.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "prereview_auto_dismiss", True, raising=False)
    monkeypatch.setattr(s, "prereview_auto_clear", True, raising=False)
    monkeypatch.setattr(s, "prereview_auto_dismiss_min_sample", 3, raising=False)

    # gated BLOCK 룰 + (룰,코드) 인간 전건 DISMISS 3건 — 비-gated였다면 패턴화될 표본
    session.add(PreReviewRule(rule_type="EXCLUSIVE_PAIR", code="R-GATE", name="EXCLUSIVE_PAIR",
                              severity="BLOCK", physician_gated=True))
    c = PreReviewCase(chart_no="C-GHIST", care_month="2026-05", status="CLEARED")
    session.add(c)
    await session.flush()
    for _ in range(3):
        session.add(PreReviewFinding(case_id=c.id, rule_code="R-GATE", code="ZZ100",
                                     severity="BLOCK", decision="DISMISS", decided_by=9))
    await session.flush()
    # 1차 방어 — gated는 자동 오탐 패턴에서 제외
    pats = await svc._auto_dismiss_patterns(session, min_sample=3)
    assert ("R-GATE", "ZZ100") not in pats

    # 신규 gated 적발 → 자동 DISMISS 안 됨 → live 잔존 → 자동 CLEARED 발화 안 함
    gf = SimpleNamespace(chart_no="C-GNEW", rule_code="R-GATE", rule_name="EXCLUSIVE_PAIR",
                         severity="BLOCK", code="ZZ100", treated_on=date(2026, 6, 1),
                         detail="상호배타 동시청구")
    lines = [_ln(chart="C-GNEW", code="ZZ100", amount=8_000)]
    out = await svc.save_run(session, actor=1, lines=lines, findings=[gf])
    assert out["auto_dismissed"] == 0 and out["auto_cleared"] == 0
    d = await svc.detail(session, case_id=out["cases"][0]["case_id"])
    assert d["status"] == "OPEN" and d["finding_count"] == 1        # 자동 종결 안 됨(의료진 확인 대기)


async def test_risk_amount_sums_no_code_findings_independently(session):
    """위험액 집계 — 무코드 적발은 각각 독립 위험(과소집계 방지)·동일 코드 적발은 코드별 1회.

    기존 `{f.code or "": amt}` dict는 무코드(code=None→"") 적발을 한 키로 붕괴시켜 마지막 1건만
    남기던 과소집계 버그(코드 dedup 의도는 유지). save_run 경로는 무코드 amount를 0으로 파생하므로
    직접 생성 적발(자동수확·수기 등)에서 발현.
    """
    from erp.claims.models import PreReviewCase, PreReviewFinding
    c = PreReviewCase(chart_no="C-RISK", care_month="2026-06", status="OPEN")
    session.add(c)
    await session.flush()
    session.add_all([
        PreReviewFinding(case_id=c.id, rule_code="R-A", rule_name="A", severity="WARN",
                         code=None, amount=100),
        PreReviewFinding(case_id=c.id, rule_code="R-B", rule_name="B", severity="WARN",
                         code=None, amount=200),          # 무코드 2건 → 독립 합산
        PreReviewFinding(case_id=c.id, rule_code="R-C", rule_name="C", severity="WARN",
                         code="AA100", amount=50),
        PreReviewFinding(case_id=c.id, rule_code="R-D", rule_name="D", severity="WARN",
                         code="AA100", amount=50),          # 동일 코드 → 노출 1회
    ])
    await session.flush()
    await svc._refresh_rollup(session, c)
    assert c.finding_count == 4
    # 무코드 100+200(독립) + 코드 AA100 50(중복 1회) = 350 (버그 시 250으로 과소집계)
    assert float(c.risk_amount) == 350


async def test_finding_decision_dismiss_requires_note(session):
    lines = [_ln(amount=3_000), _ln(amount=3_000)]
    out = await svc.save_run(session, actor=1, lines=lines, findings=_findings(lines))
    fid = (await svc.detail(session, case_id=out["cases"][0]["case_id"]))["findings"][0]["id"]

    with pytest.raises(ValidationError, match="사유"):
        await svc.decide_finding(session, actor=1, finding_id=fid, decision="DISMISS")
    f = await svc.decide_finding(session, actor=1, finding_id=fid,
                                 decision="DISMISS", note="동일코드 분할 청구 정당")
    assert f.decision == "DISMISS"
    # 오탐 처리 → 위험액·적발 수 롤업에서 제외
    detail = await svc.detail(session, case_id=f.case_id)
    assert detail["finding_count"] == 0 and detail["risk_amount"] == 0


async def _gated_case(session, rule_type, code):
    from erp.claims.models import PreReviewCase, PreReviewFinding, PreReviewRule
    session.add(PreReviewRule(rule_type=rule_type, code=code, name=rule_type,
                              severity="WARN", physician_gated=True))
    c = PreReviewCase(chart_no=f"C-{code}", care_month="2026-06", status="IN_REVIEW")
    session.add(c)
    await session.flush()
    f = PreReviewFinding(case_id=c.id, rule_code=code, rule_name=rule_type,
                         severity="WARN", code="AA1", amount=10_000)
    session.add(f)
    await session.flush()
    return c, f


async def test_physician_gated_blocks_solo_dismiss(session):
    """의료법 — 의학판단 룰(physician_gated) 적발은 심사자 단독 오탐 금지. ACCEPT(에스컬레이션)은 허용."""
    _c, f = await _gated_case(session, "EXCLUSIVE_PAIR", "R-EXCL")
    with pytest.raises(ValidationError, match="의학판단"):
        await svc.decide_finding(session, actor=1, finding_id=f.id, decision="DISMISS", note="x")
    fa = await svc.decide_finding(session, actor=1, finding_id=f.id, decision="ACCEPT")
    assert fa.decision == "ACCEPT"
    # detail에 physician_gated 노출(웹이 오탐 버튼 숨김)
    d = await svc.detail(session, case_id=f.case_id)
    assert d["findings"][0]["physician_gated"] is True


async def _medical_employee(session, no="DR-01"):
    from erp.hr.models import Employee
    e = Employee(employee_no=no, name="의사", source="HIS", hired_on=date(2026, 1, 1),
                 staff_class="MEDICAL", job_category="DOCTOR")
    session.add(e)
    await session.flush()
    return e


async def test_physician_gated_resolve_requires_physician_confirmation(session):
    """의학판단 적발 종결 — 실존 의료진 + 의료진 회신 강제. 충족 시 DISMISS·확인의 기록."""
    from erp.claims.models import PreReviewFinding, PreReviewQuery
    c, f = await _gated_case(session, "PERIOD_LIMIT", "R-PER")
    doc = await _medical_employee(session)
    q = await svc.create_query(session, actor=1, case_id=c.id,
                               question="기간 내 재투여 사유?", finding_id=f.id, assigned_to=77)
    with pytest.raises(ValidationError, match="책임 의료진"):       # 확인의 없음
        await svc.resolve_query(session, actor=1, query_id=q.id, status="ACCEPTED", note="인정")
    await svc.reply_query(session, actor=1, query_id=q.id, body="진단상 적정", reply_type="EVIDENCE")
    out = await svc.resolve_query(session, actor=1, query_id=q.id, status="ACCEPTED",
                                  note="의료진 근거 인정", confirmed_by_employee=doc.id)
    assert out["dismissed_finding"] == f.id
    fr = await session.get(PreReviewFinding, f.id)
    assert fr.decision == "DISMISS" and fr.decided_at is not None   # 감사 정렬용 시각 기록
    qr = await session.get(PreReviewQuery, q.id)
    assert qr.confirmed_by_employee == doc.id


async def test_physician_gated_resolve_rejects_non_medical_confirmer(session):
    """백도어 차단 — 회신이 있어도 확인의가 실존 의료진(MEDICAL)이 아니면 gated 종결 거부.

    비의료인 심사자가 임의 정수/비의료 직원을 확인의로 참칭해 자가 종결하던 우회 경로 차단
    (decide_finding 단독 DISMISS 금지와 대칭).
    """
    from erp.hr.models import Employee
    c, f = await _gated_case(session, "EXCLUSIVE_PAIR", "R-EXC2")
    q = await svc.create_query(session, actor=1, case_id=c.id,
                               question="동시청구 사유?", finding_id=f.id)
    await svc.reply_query(session, actor=1, query_id=q.id, body="자가회신", reply_type="EVIDENCE")
    # (a) 존재하지 않는 employee id
    with pytest.raises(ValidationError, match="실존 의료진"):
        await svc.resolve_query(session, actor=1, query_id=q.id, status="ACCEPTED",
                                note="x", confirmed_by_employee=999999)
    # (b) 실존하나 비의료(SUPPORT) 직원
    sup = Employee(employee_no="SUP-1", name="원무", source="ERP", hired_on=date(2026, 1, 1),
                   staff_class="SUPPORT", job_category="STAFF")
    session.add(sup)
    await session.flush()
    with pytest.raises(ValidationError, match="실존 의료진"):
        await svc.resolve_query(session, actor=1, query_id=q.id, status="ACCEPTED",
                                note="x", confirmed_by_employee=sup.id)
    # 적발은 여전히 미해소(자가 종결 안 됨)
    fr = await session.get(type(f), f.id)
    assert fr.decision == "PENDING"


async def test_create_query_rejects_foreign_finding(session):
    """적발-케이스 정합 — 타 케이스 적발을 질의로 엮으면 거부(롤업 오염·교차종결 차단)."""
    from erp.claims.models import PreReviewCase, PreReviewFinding
    ca = PreReviewCase(chart_no="C-QA", care_month="2026-06", status="OPEN")
    cb = PreReviewCase(chart_no="C-QB", care_month="2026-06", status="OPEN")
    session.add_all([ca, cb])
    await session.flush()
    fb = PreReviewFinding(case_id=cb.id, rule_code="R-X", rule_name="X", severity="WARN",
                          code="BB1", amount=1_000)
    session.add(fb)
    await session.flush()
    with pytest.raises(ValidationError, match="속하지 않"):
        await svc.create_query(session, actor=1, case_id=ca.id,
                               question="?", finding_id=fb.id)


# ── PR-B: 통계 이상치·삭감 컨텍스트·통계·일괄 반영 ────────
def test_stat_outlier_batch_percentile():
    rules = [_rule("STAT_OUTLIER", {"percentile": 90, "factor": 1.5, "min_samples": 10})]
    lines = [_ln(chart=f"C{i}", amount=10_000) for i in range(11)] \
        + [_ln(chart="CX", amount=50_000)]                    # p90(10,000)×1.5 초과
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    assert len(out) == 1 and out[0].chart_no == "CX"
    # 표본 부족 코드는 미적발
    few = [_ln(chart=f"C{i}", code="YY", amount=10_000 * (i + 1)) for i in range(3)]
    assert PreReviewEngine().evaluate(lines=few, rules=rules, fee={}) == []


async def test_adjustment_history_context(session):
    from erp.claims.models import ClaimAdjustment, ClaimBatch, ClaimStatement
    batch = ClaimBatch(receipt_no="R-1", care_month="2026-05",
                       notice_arrived_on=date(2026, 6, 1))
    session.add(batch)
    await session.flush()
    stmt = ClaimStatement(batch_id=batch.id, stmt_serial="0001", chart_no="C-HIST")
    session.add(stmt)
    await session.flush()
    session.add_all([
        ClaimAdjustment(statement_id=stmt.id, adj_amt=30_000, adj_category="산정기준"),
        ClaimAdjustment(statement_id=stmt.id, adj_amt=20_000, adj_category="산정기준"),
    ])
    await session.flush()

    hist = await svc.adjustment_history(session, chart_no="C-HIST")
    assert hist["count"] == 2 and hist["total_amt"] == 50_000
    assert hist["top_categories"][0]["category"] == "산정기준"
    # 케이스 상세에 자동 부착
    lines = [_ln(chart="C-HIST", amount=1_000), _ln(chart="C-HIST", amount=1_000)]
    out = await svc.save_run(session, actor=1, lines=lines, findings=_findings(lines))
    detail = await svc.detail(session, case_id=out["cases"][0]["case_id"])
    assert detail["adjustment_history"]["count"] == 2


async def test_stats_dismiss_rate(session):
    lines = [_ln(chart="C-ST", amount=2_000), _ln(chart="C-ST", amount=2_000)]
    out = await svc.save_run(session, actor=1, lines=lines, findings=_findings(lines))
    fid = (await svc.detail(session, case_id=out["cases"][0]["case_id"]))["findings"][0]["id"]
    await svc.decide_finding(session, actor=1, finding_id=fid,
                             decision="DISMISS", note="정당 분할")
    stats = await svc.stats(session)
    rule = next(r for r in stats["by_rule"] if r["rule_code"] == "R-DUP")
    assert rule["dismissed"] == 1 and rule["dismiss_rate"] == 1.0
    assert any(s["status"] == "OPEN" for s in stats["by_status"])


async def test_bulk_status_sheet_roundtrip(session):
    lines = [_ln(chart="C-BK", amount=1_000), _ln(chart="C-BK", amount=1_000)]
    out = await svc.save_run(session, actor=1, lines=lines, findings=_findings(lines))
    month = (await svc.detail(session, case_id=out["cases"][0]["case_id"]))["care_month"]

    res = await svc.bulk_status(session, actor=1, items=[
        {"chart_no": "C-BK", "care_month": month, "status": "IN_REVIEW"},   # 적용
        {"chart_no": "C-BK", "care_month": month, "status": "RESOLVED"},    # IN_REVIEW→RESOLVED 위반? (불허)
        {"chart_no": "NOPE", "care_month": month, "status": "CLEARED"},     # 케이스 없음
    ])
    assert res["applied"] == 1 and len(res["skipped"]) == 2


async def test_sync_from_billing_lines(session):
    """D-6 라인 미러 → 워크리스트 자동 적재 (룰 적발·케이스 생성·단가 폴백)."""
    from erp.claims.models import PreReviewRule
    from erp.integrations.inbound_consumer import _billing_line

    session.add(PreReviewRule(code="R-SYNC-DUP", name="중복", rule_type="DUPLICATE",
                              severity="BLOCK", active=True))
    # 동일 차트·코드·일자 중복 2건 (unitPrice 폴백 단가)
    for i in range(2):
        await _billing_line(session, {
            "lineId": f"L-SY-{i}", "chartNo": "C-SYNC", "code": "SY100",
            "unitPrice": 30_000, "treatedOn": "2026-06-05"},
            "billing.line_recorded", {})
    await session.flush()

    out = await svc.sync_from_billing_lines(session, actor=1, care_month="2026-06")
    assert out["lines"] == 2 and out["findings"] >= 1
    assert len(out["cases"]) == 1
    detail = await svc.detail(session, case_id=out["cases"][0]["case_id"])
    assert detail["chart_no"] == "C-SYNC" and detail["max_severity"] == "BLOCK"
    assert detail["risk_amount"] == 60_000              # 중복 2건 관련 청구액 합(폴백 단가)

    # 재실행 멱등
    out2 = await svc.sync_from_billing_lines(session, actor=1, care_month="2026-06")
    assert out2["cases"][0]["case_id"] == out["cases"][0]["case_id"]


def test_noncovered_claim_rule():
    """비급여 항목의 급여 청구 적발 (고시 전문 비급여 목록 기반, BLOCK)."""
    rules = [_rule("NONCOVERED_CLAIM", severity="BLOCK")]
    fee = {"AZ001": {"price": 0.0, "cap": None, "type": "NONCOVERED"},
           "AA100": {"price": 10_000, "cap": None, "type": "TARIFF"}}
    lines = [_ln(code="AZ001"), _ln(chart="C2", code="AA100")]
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee=fee)
    assert len(out) == 1 and out[0].code == "AZ001" and out[0].severity == "BLOCK"
    assert "급여 청구 불가" in out[0].detail


async def test_case_encounter_type_promotion(session):
    """라인 encounter_type → 케이스 승격 (단일=그대로, 혼재=MIXED, 필터)."""
    wl = WorklistService()
    rules = [_rule("DUPLICATE", severity="WARN")]
    mk = lambda chart, enc: [  # noqa: E731
        {"chart_no": chart, "code": "AA100", "count": 1, "amount": 1000,
         "treated_on": date(2026, 7, 1), "treated_at": None, "encounter_type": e}
        for e in enc] * 2          # 중복 적발 유도(케이스 생성 조건)
    f1 = PreReviewEngine().evaluate(lines=mk("C-IN", ["INPATIENT"]), rules=rules, fee={})
    await wl.save_run(session, actor=1, lines=mk("C-IN", ["INPATIENT"]), findings=f1)
    f2 = PreReviewEngine().evaluate(lines=mk("C-MIX", ["INPATIENT", "OUTPATIENT"]),
                                    rules=rules, fee={})
    await wl.save_run(session, actor=1, lines=mk("C-MIX", ["INPATIENT", "OUTPATIENT"]),
                      findings=f2)

    rows = {c["chart_no"]: c for c in await wl.list_cases(session)}
    assert rows["C-IN"]["encounter_type"] == "INPATIENT"
    assert rows["C-MIX"]["encounter_type"] == "MIXED"
    only_in = await wl.list_cases(session, encounter_type="INPATIENT")
    assert {c["chart_no"] for c in only_in} == {"C-IN"}


async def test_case_statement_groups_and_findings(session):
    """진료비 명세 — 라인 계산분해·codeType 그룹 소계·라인↔적발 매핑."""
    from erp.patient.models import BillingLine
    wl = WorklistService()
    # 라인 2건(수가·약제) — 차트 C-STMT, 2026-05
    session.add(BillingLine(line_id="L-S1", chart_no="C-STMT", code="AA257",
                            code_type="TARIFF", count=3, coverage="NHIS",
                            encounter_type="OUTPATIENT", treated_on=date(2026, 5, 10),
                            unit_price_his=85_000))
    session.add(BillingLine(line_id="L-S2", chart_no="C-STMT", code="M047802",
                            code_type="DRUG", count=2, coverage="NHIS",
                            encounter_type="OUTPATIENT", treated_on=date(2026, 5, 10),
                            unit_price_his=1_000, drug_name="아목시실린정 500mg",
                            drug_ingredient="amoxicillin"))
    await session.flush()
    # 적발(케이스) 적재
    rules = [_rule("COUNT_LIMIT", params={"AA257": 1}, severity="BLOCK")]
    lines = [{"chart_no": "C-STMT", "code": "AA257", "count": 3, "amount": 255_000,
              "treated_on": date(2026, 5, 10), "treated_at": None}]
    findings = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    await wl.save_run(session, actor=1, lines=lines, findings=findings)
    case = (await wl.list_cases(session))[0]

    st = await wl.case_statement(session, case_id=case["id"])
    assert st["summary"]["total"] == 255_000 + 2_000
    assert st["summary"]["patient_share"] == round(255_000 * 0.4) + round(2_000 * 0.4)
    # 1차 그룹 = 임상영역. 명칭 미해소 AA257→ETC, M047802 DRUG→투약(code_type 확정)
    gmap = {g["category"]: g for g in st["groups"]}
    assert gmap["DRUG"]["claimed"] == 2_000 and gmap["ETC"]["claimed"] == 255_000
    aa = gmap["ETC"]["lines"][0]
    assert aa["formula"] == "85,000 × 3 = 255,000 → ×40% 본인부담"
    assert len(aa["findings"]) == 1 and aa["findings"][0]["severity"] == "BLOCK"
    # 투약 라인 — 약품명·성분 노출(병용·중복투약 심사 최소필요 정보)
    drug = gmap["DRUG"]["lines"][0]
    assert drug["drug_name"] == "아목시실린정 500mg" and drug["drug_ingredient"] == "amoxicillin"
    # 급여분류 소계는 청구 관점으로 별도 보존(행위료 ACT 255,000 · 약제비 DRUG 2,000)
    bmap = {b["category"]: b["claimed"] for b in st["benefit_summary"]}
    assert bmap["ACT"] == 255_000 and bmap["DRUG"] == 2_000


async def test_collaboration_loop_dismiss_on_evidence(session):
    """삭감 방지 루프 — 적발 질의 → 의료진 근거 회신 → ACCEPTED → 적발 DISMISS 연계."""
    wl = WorklistService()
    rules = [_rule("COUNT_LIMIT", params={"AA257": 1}, severity="BLOCK")]
    lines = [{"chart_no": "C-Q", "code": "AA257", "count": 3, "amount": 255_000,
              "treated_on": date(2026, 5, 10), "treated_at": None}]
    findings = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    await wl.save_run(session, actor=1, lines=lines, findings=findings)
    case = (await wl.list_cases(session))[0]
    detail = await wl.detail(session, case_id=case["id"])
    fid = detail["findings"][0]["id"]
    assert case["finding_count"] == 1

    # 심사팀 질의 (적발 단위)
    q = await wl.create_query(session, actor=10, case_id=case["id"],
                              question="AA257 3회 산정 근거 요청", finding_id=fid,
                              assigned_to=99)
    assert q.status == "OPEN"
    # 의료진 회신 (근거)
    await wl.reply_query(session, actor=99, query_id=q.id,
                         body="응급 야간 3회 시행 — 진료기록 첨부", reply_type="EVIDENCE")
    timeline = await wl.list_queries(session, case_id=case["id"])
    assert timeline[0]["status"] == "ANSWERED" and len(timeline[0]["replies"]) == 1
    assert "chart_no" in timeline[0] and "care_month" in timeline[0]   # 관리 화면 컨텍스트 동봉
    # 전역 관리 로그 — 부서 경계: 일치 부서만, 미일치 부서는 제외(IDOR)
    glob = await wl.list_queries(session)
    assert any(x["id"] == q.id for x in glob)
    assert await wl.list_queries(session, dept_codes=["__none__"]) == []
    # 심사팀 인정 → 적발 DISMISS 연계
    out = await wl.resolve_query(session, actor=10, query_id=q.id,
                                 status="ACCEPTED", note="근거 충분")
    assert out["dismissed_finding"] == fid
    d2 = await wl.detail(session, case_id=case["id"])
    assert d2["findings"][0]["decision"] == "DISMISS"
    # 적발 해소 → 케이스 위험 제거
    c2 = (await wl.list_cases(session))[0]
    assert c2["finding_count"] == 0       # DISMISS는 live 집계 제외


async def test_query_on_line_without_finding(session):
    """적발 없는 라인에도 질의 가능(확정 ③ — 적발+라인 둘 다)."""
    wl = WorklistService()
    rules = [_rule("DUPLICATE", severity="WARN")]
    lines = [{"chart_no": "C-L", "code": "ZZ", "count": 1, "amount": 1000,
              "treated_on": date(2026, 5, 1), "treated_at": None}] * 2
    findings = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    await wl.save_run(session, actor=1, lines=lines, findings=findings)
    case = (await wl.list_cases(session))[0]
    q = await wl.create_query(session, actor=10, case_id=case["id"],
                              question="고가 비급여 확인", line_code="XX999")
    assert q.finding_id is None and q.line_code == "XX999"


def test_noncovered_excluded_from_benefit_rules():
    """비급여 라인은 급여 삭감 룰(산정횟수 등)에서 제외 — false WARN 방지."""
    rules = [_rule("COUNT_LIMIT", params={"D590": 1}, severity="WARN"),
             _rule("NONCOVERED_CLAIM", severity="BLOCK")]
    fee = {"D590": {"price": 15580, "cap": None, "type": "NONCOVERED"}}
    # 비급여 라인 2회(coverage·fee type 모두 NONCOVERED)
    lines = [{"chart_no": "C1", "code": "D590", "count": 2, "amount": 31160,
              "coverage": "NONCOVERED", "treated_on": date(2026, 5, 1)}]
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee=fee)
    # 산정횟수(급여 룰)는 적발 안 됨, 비급여 급여청구(NONCOVERED_CLAIM)는 적발
    assert not any(f.rule_name == "동일 진료일 산정횟수 초과" for f in out)
    # 급여 라인은 산정횟수 정상 적발
    cov = [{"chart_no": "C2", "code": "AA", "count": 2, "amount": 1000,
            "coverage": "NHIS", "treated_on": date(2026, 5, 1)}]
    out2 = PreReviewEngine().evaluate(
        lines=cov, rules=[_rule("COUNT_LIMIT", params={"AA": 1}, severity="WARN")],
        fee={"AA": {"price": 500, "cap": None, "type": "TARIFF"}})
    assert len(out2) == 1


# ── 보안: 부서 경계 IDOR (security-architect HIGH — 변경 EP 무가드/게이트 no-op 회귀) ──
async def test_worklist_dept_boundary_idor(session):
    """타 부서 MANAGER의 케이스 열람·상태변경·적발 DISMISS(삭감 우회)·질의종결 전면 차단.

    이전 사이클에서 읽기 EP에만 부분 적용 + save_run dept_code 미설정으로 게이트가
    no-op이던 결함(HIGH)의 회귀 방지. ALL 스코프(ADMIN)·본부서 MANAGER는 허용.
    """
    from erp.claims.models import PreReviewCase
    from erp.claims.router import (
        CaseUpdateBody, FindingDecisionBody, QueryBody, QueryResolveBody,
        create_query, resolve_query, worklist_case_detail, worklist_case_update,
        worklist_finding_decision,
    )
    from erp.common.models import Department
    from erp.common.router import CurrentUser
    from erp.errors import ForbiddenError

    # 부서 — case.dept_code(HIS 부서명)와 user.dept_id(부서 PK) 매핑(H1 해소)
    im, gs = Department(name="내과", code="IM"), Department(name="외과", code="GS")
    session.add_all([im, gs])
    await session.flush()

    def _mgr(dept_id):
        return CurrentUser(user_id=100 + dept_id, role="MANAGER", dept_id=dept_id,
                           license_flag=True)
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)

    # 라인 dept_code(부서명) → 케이스 dept_code='내과' 자동 설정
    lines = [_ln(amount=4_000, dept_code="내과"), _ln(amount=4_000, dept_code="내과")]
    out = await svc.save_run(session, actor=1, lines=lines, findings=_findings(lines))
    cid = out["cases"][0]["case_id"]
    assert (await session.get(PreReviewCase, cid)).dept_code == "내과"   # save_run 채움 확인
    fid = (await svc.detail(session, case_id=cid))["findings"][0]["id"]

    other, own = _mgr(gs.id), _mgr(im.id)   # 외과 MANAGER(타) vs 내과 MANAGER(본)

    # 타 부서 MANAGER — 상세·상태변경·적발 DISMISS 전부 ForbiddenError
    with pytest.raises(ForbiddenError, match="타 부서"):
        await worklist_case_detail(case_id=cid, user=other, session=session)
    with pytest.raises(ForbiddenError, match="타 부서"):
        await worklist_case_update(case_id=cid, body=CaseUpdateBody(status="IN_REVIEW"),
                                   user=other, session=session)
    with pytest.raises(ForbiddenError, match="타 부서"):
        await worklist_finding_decision(
            finding_id=fid, body=FindingDecisionBody(decision="DISMISS", note="우회 시도"),
            user=other, session=session)               # 삭감 우회 차단

    # 질의 종결(ACCEPTED→DISMISS 연계) 경로도 타 부서 차단
    q = await create_query(case_id=cid, body=QueryBody(question="근거?", finding_id=fid),
                           user=own, session=session)
    with pytest.raises(ForbiddenError, match="타 부서"):
        await resolve_query(query_id=q["id"],
                            body=QueryResolveBody(status="ACCEPTED", note="우회"),
                            user=other, session=session)

    # 본부서 MANAGER·ADMIN(ALL) — 허용
    assert (await worklist_case_detail(case_id=cid, user=own, session=session))["id"] == cid
    assert (await worklist_case_detail(case_id=cid, user=admin, session=session))["id"] == cid

    # dept_code 미상 케이스는 DEPT 스코프 차단(보안 기본값), ALL은 허용
    bare = PreReviewCase(chart_no="C-BARE", care_month="2026-06", status="OPEN")
    session.add(bare)
    await session.flush()
    with pytest.raises(ForbiddenError, match="타 부서"):
        await worklist_case_detail(case_id=bare.id, user=own, session=session)
    assert (await worklist_case_detail(case_id=bare.id, user=admin, session=session))["id"] == bare.id


# ── 약품 매핑된 HIS 코드는 미등재(UNKNOWN_CODE) 오탐 안 함 (운영 진단 2026-06-14) ──
async def test_sync_unknown_code_resolved_by_drug_map(session):
    """CONFIRMED 매핑된 HIS 비표준 약품코드는 사전심사 미등재 오탐 안 함 — 인보이스 매핑 일관.

    운영 사례: M047803(HIS)→648900880(보험) CONFIRMED. 매핑 약가가 진료일(5월)엔 미시행이고
    현행(valid_from 6/1·valid_to NULL)이어도, 현행 폴백으로 fee 멤버십 해소 → UNKNOWN_CODE 미발생.
    급여(NHIS) 라인 기준(비급여는 D-1로 UNKNOWN_CODE 제외). 미매핑 코드는 여전히 적발(+문구 중복 제거).
    """
    from datetime import date as _d
    from erp.claims.models import PreReviewRule
    from erp.patient.models import BillingLine
    from erp.regulatory.models import DrugCodeMap, FeeMaster

    session.add_all([
        PreReviewRule(code="R-UNK-T", name="수가 마스터 미등재 코드",
                      rule_type="UNKNOWN_CODE", severity="BLOCK", active=True),
        # 매핑 약가는 진료일(2026-05) 이후 시행(2026-06-01)·현행(valid_to NULL) — 실제 운영과 동일
        FeeMaster(code="648900880", code_type="DRUG", name="지스로맥스정 250mg",
                  unit_price=1075, valid_from=_d(2026, 6, 1)),
        DrugCodeMap(his_code="M047803", insurance_code="648900880",
                    status="CONFIRMED", drug_name="지스로맥스정 250mg"),
        BillingLine(line_id="BL-UNK-1", chart_no="C-UNK", code="M047803",
                    code_type="DRUG", count=3, coverage="NHIS",
                    unit_price_his=890, treated_on=_d(2026, 5, 28)),
        # 대조군 — 매핑 없는 급여 코드(여전히 미등재 적발)
        BillingLine(line_id="BL-UNK-2", chart_no="C-UNK2", code="ZZ999",
                    code_type="DRUG", count=1, coverage="NHIS",
                    unit_price_his=500, treated_on=_d(2026, 5, 29)),
    ])
    await session.flush()

    out = await svc.sync_from_billing_lines(session, actor=1, care_month="2026-05")
    assert out["lines"] == 2
    # 매핑 해소 → C-UNK 미등재 오탐 없음 → 적발 0 → 케이스 미생성
    assert not any(c["chart_no"] == "C-UNK" for c in out["cases"])
    # 미매핑 C-UNK2는 적발 + 문구 중복 제거 확인
    c2 = next(c for c in out["cases"] if c["chart_no"] == "C-UNK2")
    d2 = await svc.detail(session, case_id=c2["case_id"])
    unk = next(f for f in d2["findings"] if f["code"] == "ZZ999")
    assert unk["rule_name"] == "수가 마스터 미등재 코드"
    assert unk["detail"] == "청구 코드 ZZ999"          # 접두 중복 제거(과거: "수가 마스터 미등재 코드: ZZ999")


async def test_manual_pre_review_applies_drug_map(session):
    """수동 사전심사(/pre-review)도 CONFIRMED 매핑 적용 — sync와 동일 헬퍼(일관).

    매핑된 HIS 코드를 붙여넣어도 미등재(UNKNOWN_CODE) 오탐 안 함. 미매핑은 적발.
    """
    from datetime import date as _d
    from erp.claims.models import PreReviewRule
    from erp.claims.router import PreReviewBody, PreReviewLine, pre_review
    from erp.common.router import CurrentUser
    from erp.regulatory.models import DrugCodeMap, FeeMaster

    session.add_all([
        PreReviewRule(code="R-UNK-M", name="수가 마스터 미등재 코드",
                      rule_type="UNKNOWN_CODE", severity="BLOCK", active=True),
        FeeMaster(code="648900880", code_type="DRUG", name="지스로맥스정 250mg",
                  unit_price=1075, valid_from=_d(2026, 6, 1)),
        DrugCodeMap(his_code="M047803", insurance_code="648900880",
                    status="CONFIRMED", drug_name="지스로맥스정 250mg"),
    ])
    await session.flush()
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)

    body = PreReviewBody(lines=[
        PreReviewLine(chart_no="C-M", code="M047803", treated_on=_d(2026, 5, 28)),  # 매핑 → 미적발
        PreReviewLine(chart_no="C-M", code="NOPE9", treated_on=_d(2026, 5, 28)),    # 미매핑 → 적발
    ])
    out = await pre_review(body=body, user=admin, session=session)
    flagged = {f["code"] for f in out["findings"] if f["rule_code"] == "R-UNK-M"}
    assert "M047803" not in flagged          # 매핑 해소 — 오탐 없음
    assert "NOPE9" in flagged                # 미매핑은 정상 적발


async def test_sync_blood_product_exempt_from_unknown_code(session):
    """혈액제제(약가 비대상)는 미등재(UNKNOWN_CODE) 제외 — 혈액수가 체계라 약가 마스터 미등재가
    정상. 단 급여 라인이므로 다른 급여룰(중복 등)은 여전히 적용(membership-only 제외)."""
    from datetime import date as _d
    from erp.claims.models import PreReviewRule
    from erp.patient.models import BillingLine
    from erp.regulatory.models import DrugCodeMap

    session.add_all([
        PreReviewRule(code="R-UNK-B", name="수가 마스터 미등재 코드",
                      rule_type="UNKNOWN_CODE", severity="BLOCK", active=True),
        PreReviewRule(code="R-DUP-B", name="중복청구", rule_type="DUPLICATE",
                      severity="BLOCK", active=True),
        DrugCodeMap(his_code="M400101", drug_name="농축적혈구(pRBC) 320mL",
                    ingredient="적혈구", status="SUGGESTED",
                    matched_name="혈액수가 체계(약가 비대상)"),
        # 혈액 라인 2건(동일 차트·코드·일자) — 중복은 적발돼야, 미등재는 제외
        BillingLine(line_id="BL-BLD-1", chart_no="C-BLD", code="M400101", code_type="DRUG",
                    count=1, coverage="NHIS", unit_price_his=35000, treated_on=_d(2026, 5, 10)),
        BillingLine(line_id="BL-BLD-2", chart_no="C-BLD", code="M400101", code_type="DRUG",
                    count=1, coverage="NHIS", unit_price_his=35000, treated_on=_d(2026, 5, 10)),
    ])
    await session.flush()

    out = await svc.sync_from_billing_lines(session, actor=1, care_month="2026-05")
    c = next(x for x in out["cases"] if x["chart_no"] == "C-BLD")
    rules_hit = {f["rule_code"] for f in (await svc.detail(session, case_id=c["case_id"]))["findings"]}
    assert "R-UNK-B" not in rules_hit      # 혈액제제 미등재 제외
    assert "R-DUP-B" in rules_hit          # 급여 중복은 여전히 적발(membership-only 제외 확인)


async def test_statement_finding_attached_by_treated_on(session):
    """날짜특정 적발은 해당 진료일 라인에만 부착 — 다른 날짜 라인엔 안 붙음(#1 정밀화).

    운영 사례: 같은 코드가 06-10(1회)·06-11(2회)일 때 산정횟수 초과는 06-11 건인데
    종전엔 코드 기준 매핑이라 06-10 라인에도 표시됐음. treated_on으로 정확 부착.
    """
    from datetime import date as _d
    from erp.claims.models import PreReviewRule
    from erp.patient.models import BillingLine
    from erp.regulatory.models import FeeMaster

    session.add_all([
        PreReviewRule(code="R-CNT-D", name="동일 진료일 산정횟수 초과",
                      rule_type="COUNT_LIMIT", params={"max": 1}, severity="WARN", active=True),
        FeeMaster(code="KK059", code_type="TARIFF", name="정맥내유치침",
                  unit_price=520, valid_from=_d(2026, 1, 1)),
        BillingLine(line_id="BL-DT-1", chart_no="C-DT", code="KK059", code_type="TARIFF",
                    count=1, coverage="NHIS", encounter_type="OUTPATIENT", treated_on=_d(2026, 6, 10)),
        BillingLine(line_id="BL-DT-2", chart_no="C-DT", code="KK059", code_type="TARIFF",
                    count=1, coverage="NHIS", encounter_type="OUTPATIENT", treated_on=_d(2026, 6, 11)),
        BillingLine(line_id="BL-DT-3", chart_no="C-DT", code="KK059", code_type="TARIFF",
                    count=1, coverage="NHIS", encounter_type="OUTPATIENT", treated_on=_d(2026, 6, 11)),
    ])
    await session.flush()
    out = await svc.sync_from_billing_lines(session, actor=1, care_month="2026-06")
    cid = next(c["case_id"] for c in out["cases"] if c["chart_no"] == "C-DT")
    stmt = await svc.case_statement(session, case_id=cid)
    rule = "동일 진료일 산정횟수 초과"
    d10 = [ln for g in stmt["groups"] for ln in g["lines"]
           if ln.get("code") == "KK059" and str(ln.get("treated_on")) == "2026-06-10"]
    d11 = [ln for g in stmt["groups"] for ln in g["lines"]
           if ln.get("code") == "KK059" and str(ln.get("treated_on")) == "2026-06-11"]
    assert d10 and all(not any(f["rule_name"] == rule for f in ln["findings"]) for ln in d10)
    assert d11 and all(any(f["rule_name"] == rule for f in ln["findings"]) for ln in d11)


async def test_list_cases_treated_on_filter(session):
    """진료일(일 단위) 필터 — 해당 진료일에 적발 있는 케이스만(목록 드릴다운)."""
    from datetime import date as _d
    l10 = [_ln(chart="C-D10", amount=1000, treated_on=_d(2026, 6, 10)),
           _ln(chart="C-D10", amount=1000, treated_on=_d(2026, 6, 10))]
    l12 = [_ln(chart="C-D12", amount=1000, treated_on=_d(2026, 6, 12)),
           _ln(chart="C-D12", amount=1000, treated_on=_d(2026, 6, 12))]
    await svc.save_run(session, actor=1, lines=l10, findings=_findings(l10))
    await svc.save_run(session, actor=1, lines=l12, findings=_findings(l12))
    d10 = await svc.list_cases(session, treated_on=_d(2026, 6, 10))
    assert {c["chart_no"] for c in d10} == {"C-D10"}          # 06-10 적발 케이스만
    allc = await svc.list_cases(session)
    assert {"C-D10", "C-D12"} <= {c["chart_no"] for c in allc}  # 필터 없으면 전체


async def test_case_diagnoses_and_ai_review_no_key(session):
    """상병(진단) 집계 + AI 종합검토 — weve 키 없으면 graceful(available False)."""
    from datetime import date as _d
    from erp.claims.router import case_ai_review
    from erp.common.router import CurrentUser
    from erp.patient.models import BillingLine

    lines = [_ln(chart="C-DX", amount=5000, treated_on=_d(2026, 6, 5)),
             _ln(chart="C-DX", amount=5000, treated_on=_d(2026, 6, 5))]
    out = await svc.save_run(session, actor=1, lines=lines, findings=_findings(lines))
    cid = out["cases"][0]["case_id"]
    session.add_all([
        BillingLine(line_id="BL-DX1", chart_no="C-DX", code="AA100", code_type="TARIFF",
                    count=1, coverage="NHIS", treated_on=_d(2026, 6, 5), diagnosis_codes="S52.5,M84.3"),
        BillingLine(line_id="BL-DX2", chart_no="C-DX", code="AA100", code_type="TARIFF",
                    count=1, coverage="NHIS", treated_on=_d(2026, 6, 5), diagnosis_codes="S52.5"),
    ])
    await session.flush()
    dx = await svc.case_diagnoses(session, chart_no="C-DX", care_month="2026-06")
    assert set(dx) == {"S52.5", "M84.3"}          # 라인 전반 상병 중복 제거 집계
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    r = await case_ai_review(case_id=cid, user=admin, session=session)
    assert r["available"] is False and r["items"] == []   # 키 없으면 호출 안 함(graceful)


async def test_list_cases_stats_dept_scope(session):
    """부서 경계 — 목록·통계가 dept_code로 본부서만(단건 접근·반출과 일관, H2)."""
    from erp.claims.models import PreReviewCase
    session.add_all([
        PreReviewCase(chart_no="C-DEPT1", care_month="2026-06", status="OPEN",
                      risk_amount=1000, dept_code="내과"),
        PreReviewCase(chart_no="C-DEPT2", care_month="2026-06", status="OPEN",
                      risk_amount=2000, dept_code="외과"),
    ])
    await session.flush()
    charts_all = {c["chart_no"] for c in await svc.list_cases(session)}      # 전체(ALL 스코프)
    assert {"C-DEPT1", "C-DEPT2"} <= charts_all
    d1 = await svc.list_cases(session, dept_codes=["내과"])                  # 본부서(부서명) 후보
    charts_d1 = {c["chart_no"] for c in d1}
    assert "C-DEPT1" in charts_d1 and "C-DEPT2" not in charts_d1             # 타 부서 제외
    assert await svc.list_cases(session, dept_codes=[]) == []               # 빈 후보=아무것도 안 보임(과차단)
    # 통계도 부서 경계 — dept 내과 상태분포에 외과 위험액 미포함
    st1 = await svc.stats(session, dept_codes=["내과"])
    risk_d1 = sum(s["risk_amount"] for s in st1["by_status"])
    st_all = await svc.stats(session)
    assert sum(s["risk_amount"] for s in st_all["by_status"]) >= risk_d1 + 2000


async def test_user_dept_codes_resolves_name_and_code(session):
    """_user_dept_codes — dept_id(정수PK)를 부서명·코드로 해소(dept_code 체계 불일치 H1 해소)."""
    from erp.claims.router import _user_dept_codes
    from erp.common.models import Department
    from erp.common.router import CurrentUser
    dep = Department(name="내과", code="IM")
    session.add(dep)
    await session.flush()
    mgr = CurrentUser(user_id=2, role="MANAGER", dept_id=dep.id, license_flag=True)
    codes = await _user_dept_codes(session, mgr)
    assert set(codes) == {"내과", "IM"}                      # case.dept_code(부서명/코드) 양쪽 매칭
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    assert await _user_dept_codes(session, admin) is None     # ALL 스코프=경계 없음
    orphan = CurrentUser(user_id=3, role="MANAGER", dept_id=99999, license_flag=True)
    assert await _user_dept_codes(session, orphan) == []       # 미해소=과차단 안전


def test_a3_false_positive_candidate_and_draft():
    """A3 — AI 삭감위험 낮음 = 오탐 후보 + 사유 초안(휴먼인더루프, DISMISS 사유 prefill)."""
    from erp.claims.router import _dismiss_draft, _fp_candidate

    # risk_score(권위 신호) — ERP 소유 임계(0.4) 미만 = LOW 밴드 → 오탐 후보
    assert _fp_candidate({"risk_score": 0.2}) is True
    assert _fp_candidate({"risk_score": 0.5}) is False    # MEDIUM
    assert _fp_candidate({"risk_score": 0.9}) is False    # HIGH
    assert _fp_candidate({"risk_score": 0.39}) is True
    # H1 — 외부가 내려준 risk_thresholds는 신뢰하지 않음(고위험 건 오탐 둔갑 차단)
    assert _fp_candidate({"risk_score": 0.8, "risk_thresholds": {"medium": 0.99}}) is False
    # H2 — 비정상 score 방어(NaN·범위초과·bool은 score로 취급 안 함)
    assert _fp_candidate({"risk_score": float("nan")}) is False
    assert _fp_candidate({"risk_score": -1}) is True      # 클램프 0.0 < 0.4
    assert _fp_candidate({"risk_score": 5}) is False      # 클램프 1.0
    assert _fp_candidate({"risk_score": True}) is False    # bool은 폴백으로(문자열 없음→False)
    # 문자열 risk_level 폴백 (score 없을 때, 대문자/대체키)
    assert _fp_candidate({"risk_level": "LOW"}) is True
    assert _fp_candidate({"risk_level": "없음"}) is True
    assert _fp_candidate({"risk": "NONE"}) is True
    assert _fp_candidate({"risk_level": "high"}) is False
    assert _fp_candidate({}) is False

    # 초안: 실 weve 응답 형태(risk_score + evidence effective_from)
    draft = _dismiss_draft({
        "risk_score": 0.2, "recommendation": "산정기준 충족 — 정상 청구로 판단",
        "evidence_citations": [{"title": "행위료 산정지침", "effective_from": "2026-01-01"}]})
    assert "오탐 후보" in draft and "정상 청구" in draft
    assert "score 0.20" in draft                          # 점수 투명 표기
    assert "근거: 행위료 산정지침(2026-01-01)" in draft   # effective_from 인용
    assert "검토자 확인" in draft
    assert len(draft) <= 300                              # decision_note 안전 절단
    # 근거/권고 없어도 안전
    assert _dismiss_draft({"risk_score": 0.1}).startswith("AI 분석")


async def test_bulk_status_dept_scope(session):
    """P0-1 일괄경로 부서 경계 — dept_codes 지정 시 타부서 케이스는 스킵(IDOR 차단)."""
    from erp.claims.models import PreReviewCase
    session.add_all([
        PreReviewCase(chart_no="C-BLK1", care_month="2026-06", status="OPEN", dept_code="내과"),
        PreReviewCase(chart_no="C-BLK2", care_month="2026-06", status="OPEN", dept_code="외과"),
    ])
    await session.flush()
    items = [{"chart_no": "C-BLK1", "care_month": "2026-06", "status": "CLEARED"},
             {"chart_no": "C-BLK2", "care_month": "2026-06", "status": "CLEARED"}]
    out = await svc.bulk_status(session, actor=1, items=items, dept_codes=["내과"])
    assert out["applied"] == 1                                   # 내과만 반영
    assert any(s["reason"] == "타 부서 케이스" for s in out["skipped"])  # 외과 스킵


async def test_rule_validity_window_filters_expired(session):
    """P1 G7 — 진료(기준)일에 유효하지 않은 룰(valid_to 과거)은 평가 제외."""
    from datetime import date as _date

    from sqlalchemy import select

    from erp.claims.models import PreReviewCase, PreReviewFinding, PreReviewRule
    from erp.patient.models import BillingLine
    session.add_all([
        BillingLine(line_id="VW-1", chart_no="C-VW", code="ZZ1", code_type="TARIFF",
                    count=1, coverage="NHIS", treated_on=_date(2026, 6, 3), unit_price_his=1000),
        BillingLine(line_id="VW-2", chart_no="C-VW", code="ZZ1", code_type="TARIFF",
                    count=1, coverage="NHIS", treated_on=_date(2026, 6, 3), unit_price_his=1000),
    ])
    # 폐기된 룰(2025년까지만 유효) — 2026-06 sync에서 제외돼 중복 적발 없어야 함
    session.add(PreReviewRule(rule_type="DUPLICATE", code="R-DUP-OLD", name="중복(구고시)",
                              severity="WARN", active=True, valid_to=_date(2025, 12, 31)))
    await session.flush()
    await svc.sync_from_billing_lines(session, actor=0, care_month="2026-06", chart_no="C-VW")
    case = (await session.scalars(select(PreReviewCase).where(
        PreReviewCase.chart_no == "C-VW"))).first()
    findings = (await session.scalars(select(PreReviewFinding).where(
        PreReviewFinding.case_id == case.id))).all() if case else []
    assert findings == []          # 폐기 룰 제외 → 적발 없음
    # 현행 룰(valid_from 과거·valid_to NULL)이면 적발됨
    session.add(PreReviewRule(rule_type="DUPLICATE", code="R-DUP-CUR", name="중복(현행)",
                              severity="WARN", active=True, valid_from=_date(2026, 1, 1)))
    await session.flush()
    await svc.sync_from_billing_lines(session, actor=0, care_month="2026-06", chart_no="C-VW")
    case = (await session.scalars(select(PreReviewCase).where(
        PreReviewCase.chart_no == "C-VW"))).first()
    findings = (await session.scalars(select(PreReviewFinding).where(
        PreReviewFinding.case_id == case.id))).all()
    assert any(f.rule_code == "R-DUP-CUR" for f in findings)


async def test_triage_ranks_by_confirmed_accept_rate(session):
    """P2-a — 확정 결정 인정률 기반 삭감위험 triage 순위·예측삭감."""
    from erp.claims.models import PreReviewCase, PreReviewFinding
    # 이력: (R-A,X) ACCEPT3/DISMISS1=0.75, (R-B,Y) ACCEPT0/DISMISS3=0.0
    hist = PreReviewCase(chart_no="C-HIST", care_month="2026-05", status="CLEARED")
    session.add(hist)
    await session.flush()
    for d in (["ACCEPT"] * 3 + ["DISMISS"]):
        session.add(PreReviewFinding(case_id=hist.id, rule_code="R-A", code="X", severity="WARN",
                                     amount=0, decision=d, resolved=True, auto_decided=False))
    for _ in range(3):
        session.add(PreReviewFinding(case_id=hist.id, rule_code="R-B", code="Y", severity="WARN",
                                     amount=0, decision="DISMISS", resolved=True, auto_decided=False))
    # 미결 케이스 2건
    c1 = PreReviewCase(chart_no="C-T1", care_month="2026-06", status="OPEN", risk_amount=10000)
    c2 = PreReviewCase(chart_no="C-T2", care_month="2026-06", status="OPEN", risk_amount=10000)
    session.add_all([c1, c2])
    await session.flush()
    session.add(PreReviewFinding(case_id=c1.id, rule_code="R-A", code="X", severity="WARN",
                                 amount=10000, decision="PENDING", resolved=False))
    session.add(PreReviewFinding(case_id=c2.id, rule_code="R-B", code="Y", severity="WARN",
                                 amount=10000, decision="PENDING", resolved=False))
    await session.flush()
    out = await svc.triage(session, limit=10)
    items = {i["chart_no"]: i for i in out["items"]}
    assert items["C-T1"]["predicted_reduction"] == 7500   # 10000×0.75
    assert items["C-T2"]["predicted_reduction"] == 0      # 10000×0.0
    assert out["items"][0]["chart_no"] == "C-T1"          # 위험 높은 순


async def test_resolve_fee_applies_surcharge(session):
    """G6 — 종별가산율 적용: 행위료(TARIFF) 단가에 가산, 약가(DRUG) 미적용·미적재 시 무영향."""
    from datetime import date as _date

    from erp.claims.worklist_service import resolve_fee_with_mapping
    from erp.regulatory.models import FeeMaster, InstitutionSurchargeRate
    session.add_all([
        FeeMaster(code="ACT1", code_type="TARIFF", name="행위", unit_price=10000,
                  valid_from=_date(2026, 1, 1)),
        FeeMaster(code="DRG1", code_type="DRUG", name="약", unit_price=5000,
                  valid_from=_date(2026, 1, 1)),
        InstitutionSurchargeRate(grade="HOSPITAL", rate=0.2, applies_to="TARIFF"),
    ])
    await session.flush()
    as_of = _date(2026, 6, 15)
    # 가산 적용(HOSPITAL 20%) — 행위료만
    fee = await resolve_fee_with_mapping(session, {"ACT1", "DRG1"}, as_of, grade="HOSPITAL")
    assert fee["ACT1"]["price"] == 12000     # 10000 × 1.2
    assert fee["DRG1"]["price"] == 5000      # 약가 미적용
    # 종별 미지정 → 무영향
    base = await resolve_fee_with_mapping(session, {"ACT1"}, as_of)
    assert base["ACT1"]["price"] == 10000
    # 미적재 종별 → 무영향
    none_g = await resolve_fee_with_mapping(session, {"ACT1"}, as_of, grade="SUPERIOR")
    assert none_g["ACT1"]["price"] == 10000


async def test_p2_auto_dismiss_excludes_physician_gated(session):
    """CRITICAL 회귀 가드 — physician_gated 룰은 인간 DISMISS 표본이 쌓여도 자동 오탐 패턴화 금지.

    의료법·#315: 의학판단 적발은 매 건 의료진 확인이 법적 요건. 과거 확인 이력(인간 DISMISS)으로
    미래 적발을 자동 종결하면 확인 없이 오탐 처리가 되어 게이트 우회(리뷰 CRITICAL). gated 룰은
    _auto_dismiss_patterns에서 전면 제외되어야 한다(비-gated는 정상 패턴화 — 대조군)."""
    from erp.claims.models import PreReviewCase, PreReviewFinding, PreReviewRule
    session.add_all([
        PreReviewRule(rule_type="DUPLICATE", code="R-GATED", name="의학판단룰",
                      severity="WARN", active=True, physician_gated=True),
        PreReviewRule(rule_type="DUPLICATE", code="R-PLAIN", name="비의학룰",
                      severity="WARN", active=True, physician_gated=False),
    ])
    c = PreReviewCase(chart_no="C-GATE", care_month="2026-05", status="CLEARED")
    session.add(c)
    await session.flush()
    # 각 룰에 인간 DISMISS 4건(min_sample 3 초과) — gated는 의료진 확인 경유 종결분 가정
    for _ in range(4):
        session.add(PreReviewFinding(case_id=c.id, rule_code="R-GATED", code="MM100",
                                     severity="WARN", decision="DISMISS", decided_by=9))
        session.add(PreReviewFinding(case_id=c.id, rule_code="R-PLAIN", code="DD100",
                                     severity="WARN", decision="DISMISS", decided_by=9))
    await session.flush()
    pats = await svc._auto_dismiss_patterns(session, min_sample=3)
    assert ("R-GATED", "MM100") not in pats     # gated → 자동 패턴 제외(수정 후)
    assert ("R-PLAIN", "DD100") in pats          # 비-gated → 정상 패턴화(대조군)


async def test_decide_finding_records_before_on_redecision(session):
    """재결정 감사 — 기존 결정(자동 DISMISS 등) 위 수동 재결정 시 before 기록·auto_decided 해제."""
    from sqlalchemy import select

    from erp.audit.models import AuditLog
    from erp.claims.models import PreReviewCase, PreReviewFinding
    c = PreReviewCase(chart_no="C-RD", care_month="2026-06", status="OPEN")
    session.add(c)
    await session.flush()
    f = PreReviewFinding(case_id=c.id, rule_code="R-X", code="ZZ100", severity="WARN",
                         decision="DISMISS", auto_decided=True, decided_by=0)
    session.add(f)
    await session.flush()
    # 심사자가 자동 DISMISS를 ACCEPT로 재결정
    await svc.decide_finding(session, actor=7, finding_id=f.id, decision="ACCEPT", note="실제 삭감")
    await session.refresh(f)
    assert f.decision == "ACCEPT" and f.auto_decided is False and f.decided_by == 7
    log = (await session.execute(select(AuditLog).where(
        AuditLog.entity == "claims.pre_review_findings",
        AuditLog.target_id == str(f.id)).order_by(AuditLog.id.desc()))).scalars().first()
    assert log.before is not None and log.before["decision"] == "DISMISS"   # 이전 결정 기록
    assert log.before["auto_decided"] is True


# ── 산정횟수 정규화(취소 0·조정 음수) — period_limit·los_ratio (_count_limit 동형·회귀) ──
def test_period_limit_normalizes_count():
    """PERIOD — 취소(count=0) 라인이 상한 부풀리지 않고(오탐 방지), 음수 조정이 초과분 상쇄 안 함(미탐 방지)."""
    rules = [_rule("PERIOD_LIMIT", {"max": 1, "window": "M"})]
    # 취소 라인 0×2 → 산정 0회 → 무적발(수정 전: or 1로 2회 오탐)
    zero = [_ln(count=0), _ln(count=0)]
    assert PreReviewEngine().evaluate(lines=zero, rules=rules, fee={}) == []
    # count=2 + 조정 -1 → 실 초과 2>1 유지(음수 제외) → 적발(수정 전: 합 1로 미탐)
    adj = [_ln(count=2), _ln(count=-1)]
    out = PreReviewEngine().evaluate(lines=adj, rules=rules, fee={})
    assert len(out) == 1 and "산정 2회" in out[0].detail


def test_los_ratio_normalizes_count():
    """LOS — 취소(count=0) 라인이 일별 행위량 부풀리지 않음(오탐 방지)."""
    rules = [_rule("LOS_RATIO", {"per_day_max": 2})]
    # count=0 라인 5건 → 0건 → 무적발(수정 전: 5건 오탐)
    zero = [_ln(code=f"X{i}", count=0) for i in range(5)]
    assert PreReviewEngine().evaluate(lines=zero, rules=rules, fee={}) == []
    # 정상 3건(각 count=1) → 상한 2 초과 적발(정상 동작 유지)
    normal = [_ln(code=f"Y{i}", count=1) for i in range(3)]
    out = PreReviewEngine().evaluate(lines=normal, rules=rules, fee={})
    assert len(out) == 1 and "행위량 3건" in out[0].detail


def test_evaluate_isolates_failing_rule():
    """룰 예외 격리 — 한 룰(라인 타입 이상)이 실패해도 다른 룰 findings는 생성(전 배치 미탐 방지)."""
    # treated_on 문자열 → _period_limit는 d.year서 크래시, _duplicate는 str() 수용
    lines = [_ln(treated_on="2026-06-01"), _ln(treated_on="2026-06-01")]   # 같은 코드/날짜 중복
    rules = [_rule("PERIOD_LIMIT", {"max": 1}), _rule("DUPLICATE", code="R-DUP")]
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    assert any(f.rule_code == "R-DUP" for f in out)     # DUPLICATE 살아남음(PERIOD 격리)


def test_surcharge_holiday_boundary():
    """공휴일 판정 경계 — 일요일=True·토요일=False(별도 토요가산)·고정공휴=True·대체공휴=True·평일=False."""
    from erp.claims.pre_review import _is_surcharge_holiday
    assert _is_surcharge_holiday(date(2026, 6, 7)) is True     # 일요일
    assert _is_surcharge_holiday(date(2026, 6, 13)) is False   # 토요일
    assert _is_surcharge_holiday(date(2026, 1, 1)) is True     # 신정(고정공휴)
    assert _is_surcharge_holiday(date(2026, 2, 17)) is True    # 설날(연도별·대체)
    assert _is_surcharge_holiday(date(2026, 6, 1)) is False    # 평일(월)


def test_age_sex_limit_boundary():
    """연령 경계 — min/max 포함성(age==min·max 무적발, min-1·max+1 적발)."""
    rules = [_rule("AGE_SEX_LIMIT", {"code": "AA100", "min_age": 18, "max_age": 65})]

    def _n(**kw):
        return len(PreReviewEngine().evaluate(lines=[_ln(**kw)], rules=rules, fee={}))
    assert _n(age=18) == 0     # 하한 포함(적정)
    assert _n(age=17) == 1     # 하한 미달
    assert _n(age=65) == 0     # 상한 포함(적정)
    assert _n(age=66) == 1     # 상한 초과


def test_surcharge_night_hour_boundary():
    """야간 경계 — hour>=20·hour<8 야간(적정), 08시·14시·19시 평일 주간 적발(2026-06-01 월요일)."""
    rules = [_rule("SURCHARGE_CHECK",
                   {"surcharge_suffix": "010", "night_start": 20, "night_end": 8})]

    def _n(h):
        return len(PreReviewEngine().evaluate(
            lines=[_ln(code="AA010", treated_at=datetime(2026, 6, 1, h, 0))],
            rules=rules, fee={}))
    assert _n(20) == 0    # 20시 = 야간 시작(포함) → 적정
    assert _n(7) == 0     # 07시 = 야간(<8) → 적정
    assert _n(8) == 1     # 08시 = 주간(야간 아님) → 적발
    assert _n(14) == 1    # 14시 평일 주간 → 적발
    assert _n(19) == 1    # 19시 = 야간 아님 → 적발


def test_period_limit_month_window():
    """PERIOD 월 윈도우 — 같은 달 산정 합이 상한 초과 시 적발(다음달은 별도 집계)."""
    rules = [_rule("PERIOD_LIMIT", {"max": 2, "window": "M"})]
    lines = [_ln(treated_on=date(2026, 6, d)) for d in (1, 10, 20)] \
        + [_ln(treated_on=date(2026, 7, 1))]        # 7월은 별도
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    assert len(out) == 1 and "6월".replace("6월", "2026-06") in out[0].detail
    assert "월 산정 3회" in out[0].detail


def test_period_limit_week_window_year_boundary():
    """PERIOD 주 윈도우 — ISO주 연말경계(2026-12-28~2027-01-03은 동일 ISO주 2026-W53)."""
    rules = [_rule("PERIOD_LIMIT", {"max": 1, "window": "W"})]
    # 2026-12-31(목)·2027-01-01(금)은 같은 ISO주 → 합산 2회 > 1 적발(순진한 d.year면 분리·미탐)
    lines = [_ln(treated_on=date(2026, 12, 31)), _ln(treated_on=date(2027, 1, 1))]
    out = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    assert len(out) == 1 and "주 산정 2회" in out[0].detail


async def test_case_close_blocked_by_open_gated_warn_finding(session):
    """2026-10-09 검토 B — WARN 등급 의학판단 적발이 남은 케이스는 CLEARED로 덮을 수 없다(의료법)."""
    c, _f = await _gated_case(session, "AGE_SEX_LIMIT", "R-AGEG")
    c.finding_count, c.max_severity = 1, "WARN"
    await session.flush()
    with pytest.raises(ValidationError, match="의료진 확인"):
        await svc.update_case(session, actor=1, case_id=c.id, status="CLEARED")


async def test_sync_code_ended_mid_month_not_unknown_for_earlier_lines(session):
    """2026-10-09 검토 B — 월중 종료 코드(10/15까지)의 10/3 진료 라인은 UNKNOWN 오탐이 아니다.
    종전엔 월 전체를 마지막 진료일(10/25) 하나로 평가해 BLOCK이 나 케이스 종결을 막았다."""
    from datetime import date as _d

    from erp.claims.models import PreReviewRule
    from erp.patient.models import BillingLine
    from erp.regulatory.models import FeeMaster
    session.add_all([
        PreReviewRule(code="R-UNK-M", name="수가 마스터 미등재 코드",
                      rule_type="UNKNOWN_CODE", severity="BLOCK", active=True),
        FeeMaster(code="AA100", code_type="TARIFF", name="종료 수가", unit_price=1000,
                  valid_from=_d(2026, 1, 1), valid_to=_d(2026, 10, 15)),
        FeeMaster(code="BB200", code_type="TARIFF", name="현행 수가", unit_price=500,
                  valid_from=_d(2026, 1, 1)),
        BillingLine(line_id="BL-MM-1", chart_no="C-MM", code="AA100", code_type="TARIFF",
                    count=1, coverage="NHIS", unit_price_his=1000, treated_on=_d(2026, 10, 3)),
        BillingLine(line_id="BL-MM-2", chart_no="C-MM", code="BB200", code_type="TARIFF",
                    count=1, coverage="NHIS", unit_price_his=500, treated_on=_d(2026, 10, 25)),
    ])
    await session.flush()
    out = await svc.sync_from_billing_lines(session, actor=1, care_month="2026-10")
    assert not any(c["chart_no"] == "C-MM" for c in out["cases"])



async def test_dismissed_gated_finding_does_not_block_close(session):
    """2차 검토 B — 의료진 확인으로 DISMISS된 gated 적발은 종결 가드에서 제외."""
    c, f = await _gated_case(session, "AGE_SEX_LIMIT", "R-AGED")
    f.decision = "DISMISS"
    c.finding_count, c.max_severity = 1, "WARN"
    await session.flush()
    await svc.update_case(session, actor=1, case_id=c.id, status="CLEARED")
