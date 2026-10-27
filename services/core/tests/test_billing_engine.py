"""진료비 책정 엔진 (D-6) — 본인부담 분리·중간 진료비·라인 수신 치환."""
from datetime import date

from erp.integrations.inbound_consumer import _billing_line
from erp.patient.billing_engine import compute_invoice, split_line
from erp.patient.billing_service import BillingService
from erp.patient.models import BillingLine, Patient, Payment, Reception
from erp.regulatory.models import FeeMaster

svc = BillingService()


# ── 분리 엔진 (순수) ──────────────────────────────────────
def test_split_rates_by_coverage_and_encounter():
    assert split_line(total=100_000, encounter_type="INPATIENT")["patient"] == 20_000
    assert split_line(total=100_000, encounter_type="OUTPATIENT")["patient"] == 40_000
    assert split_line(total=100_000, encounter_type="INPATIENT",
                      is_meal=True)["patient"] == 50_000
    assert split_line(total=100_000, encounter_type="INPATIENT",
                      special_case=True)["patient"] == 5_000
    nc = split_line(total=100_000, coverage="NONCOVERED")
    assert nc["patient"] == 100_000 and nc["non_covered"] == 100_000
    assert split_line(total=100_000, coverage="AUTO")["patient"] == 0
    assert split_line(total=100_000, coverage="MEDICAL_AID")["patient"] == 0
    # 기관 종별 조정 — 의원 외래 30%
    assert split_line(total=100_000, rates={"OUTPATIENT": 0.30})["patient"] == 30_000


def test_compute_invoice_totals_and_balance():
    lines = [
        {"code": "AA100", "unit_price": 50_000, "count": 2,
         "coverage": "NHIS", "encounter_type": "INPATIENT"},          # 본인 20,000
        {"code": "NC900", "unit_price": 30_000, "count": 1,
         "coverage": "NONCOVERED", "encounter_type": "INPATIENT"},    # 본인 30,000
        {"code": "ZZ999", "unit_price": 0, "count": 1, "priced": False,
         "coverage": "NHIS", "encounter_type": "INPATIENT"},          # 단가 미해소 경고
    ]
    inv = compute_invoice(lines, prepaid=15_000)
    assert inv["total"] == 130_000
    assert inv["patient_share"] == 50_000 and inv["insurer_share"] == 80_000
    assert inv["non_covered"] == 30_000
    assert inv["balance_due"] == 35_000                # 본인부담 − 기수납
    assert len(inv["warnings"]) == 1


# ── 라인 수신 (멱등 치환·취소) ───────────────────────────
async def test_billing_line_upsert_and_cancel(session):
    p = {"lineId": "L-1", "chartNo": "C-1", "code": "AA100", "count": 2,
         "encounterType": "INPATIENT", "admittedOn": "2026-06-10",
         "treatedOn": "2026-06-15", "patientAge": 67, "patientSex": "M"}
    await _billing_line(session, p, "billing.line_recorded", {})
    await _billing_line(session, {**p, "count": 3}, "billing.line_recorded", {})  # 치환

    from sqlalchemy import select
    rows = (await session.scalars(select(BillingLine))).all()
    assert len(rows) == 1 and float(rows[0].count) == 3

    await _billing_line(session, {**p, "cancelled": True}, "billing.line_recorded", {})
    assert (await session.scalar(select(BillingLine.cancelled))) is True


# ── 인보이스 서비스 (시점단가·기수납·사전심사 신호) ───────
async def test_interim_invoice_end_to_end(session):
    session.add(FeeMaster(code="AA100", code_type="TARIFF", name="처치료",
                          unit_price=50_000, valid_from=date(2026, 1, 1)))
    pat = Patient(chart_no="C-INV", name="김입원", birth_date=date(1960, 1, 1))
    session.add(pat)
    await session.flush()
    rcp = Reception(patient_id=pat.id, dept_code="IM", received_on=date(2026, 6, 11))
    session.add(rcp)
    await session.flush()
    session.add(Payment(reception_id=rcp.id, amount=10_000, paid_on=date(2026, 6, 12)))

    for i, d in enumerate((12, 13)):
        await _billing_line(session, {
            "lineId": f"L-INV-{i}", "chartNo": "C-INV", "code": "AA100",
            "encounterType": "INPATIENT", "admittedOn": "2026-06-11",
            "treatedOn": f"2026-06-{d}"}, "billing.line_recorded", {})

    inv = await svc.invoice(session, chart_no="C-INV")
    assert inv["available"] and inv["total"] == 100_000
    assert inv["patient_share"] == 20_000              # 입원 20%
    assert inv["prepaid"] == 10_000 and inv["balance_due"] == 10_000
    assert inv["pre_review"]["count"] >= 0             # 동일 라인 사전심사 동봉

    # 라인 없음 → available=false
    empty = await svc.invoice(session, chart_no="C-NONE")
    assert empty["available"] is False


async def test_fee_master_missing_code_warns(session):
    await _billing_line(session, {"lineId": "L-W", "chartNo": "C-W", "code": "UNKNOWN",
                                  "treatedOn": "2026-06-15"}, "billing.line_recorded", {})
    inv = await svc.invoice(session, chart_no="C-W")
    assert inv["available"] and inv["warnings"]
    assert inv["total"] == 0


# ── coverage 매핑 (HIS enum → ERP, 회신서 §2.2) ──────────
async def test_his_coverage_enum_mapping(session):
    base = {"chartNo": "C-CV", "code": "AA100", "treatedOn": "2026-06-15"}
    cases = [("L-CV1", {"coverage": "NHI"}, "NHIS"),
             ("L-CV2", {"coverage": "MEDICAID"}, "MEDICAL_AID"),
             ("L-CV3", {"coverage": "SELF_PAY"}, "NONCOVERED"),
             ("L-CV4", {"coverage": "NHI", "insuranceCategory": "NON_COVERED"},
              "NONCOVERED"),                                   # 라인 우선
             ("L-CV5", {}, "NHIS")]                            # 미지정 기본
    for lid, extra, expected in cases:
        out = await _billing_line(session, {**base, "lineId": lid, **extra},
                                  "billing.line_recorded", {})
        assert out["coverage"] == expected, lid


async def test_his_unit_price_fallback_for_noncovered(session):
    """제안 B — 마스터 무가격(비급여)은 HIS 산정가 폴백, 마스터(>0)는 마스터 우선."""
    session.add(FeeMaster(code="BB100", code_type="TARIFF", name="처치",
                          unit_price=40_000, valid_from=date(2026, 1, 1)))
    await _billing_line(session, {
        "lineId": "L-NC1", "chartNo": "C-NC", "code": "NC500",
        "coverage": "NHI", "insuranceCategory": "NON_COVERED",
        "unitPrice": 70_000, "treatedOn": "2026-06-10"}, "billing.line_recorded", {})
    await _billing_line(session, {
        "lineId": "L-NC2", "chartNo": "C-NC", "code": "BB100",
        "unitPrice": 99_999, "treatedOn": "2026-06-10"}, "billing.line_recorded", {})
    await session.flush()

    inv = await svc.invoice(session, chart_no="C-NC")
    by_code = {ln["code"]: ln for ln in inv["lines"]}
    assert by_code["NC500"]["price_source"] == "HIS"        # 비급여 → HIS 폴백
    assert by_code["NC500"]["patient"] == 70_000            # 전액 본인부담
    assert by_code["BB100"]["price_source"] == "MASTER"     # 마스터 우선(99,999 무시)
    assert by_code["BB100"]["unit_price"] == 40_000
    assert inv["non_covered"] == 70_000


async def test_invoice_pre_review_blocks_noncovered(session):
    """H1 회귀 — invoice 경로도 fee type을 보존해 비급여 급여청구 BLOCK 신호."""
    from erp.claims.models import PreReviewRule
    session.add(PreReviewRule(code="PR-NC-T", name="비급여 급여청구",
                              rule_type="NONCOVERED_CLAIM", severity="BLOCK", active=True))
    session.add(FeeMaster(code="AZ001", code_type="NONCOVERED", name="당뇨병교육",
                          unit_price=0, valid_from=date(2026, 1, 1)))
    await _billing_line(session, {
        "lineId": "L-H1", "chartNo": "C-H1", "code": "AZ001",
        "coverage": "NHI", "unitPrice": 50_000,
        "treatedOn": "2026-06-10"}, "billing.line_recorded", {})
    await session.flush()

    inv = await svc.invoice(session, chart_no="C-H1")
    assert inv["pre_review"]["blocked"] is True            # 수납창구 신호 동작
    assert inv["lines"][0]["price_source"] == "HIS"        # 비급여 폴백가 유지


# ── 심층 재검토 보완 (본인부담 정확·식대 순서·타임존) ──────────────────
def test_meal_before_special_case_keeps_50pct():
    """식대는 산정특례 예외(50% 유지) — 특례율(5%)이 식대에 먼저 적용되던 undercharge 방지."""
    s = split_line(total=20_000, encounter_type="INPATIENT", is_meal=True, special_case=True)
    assert s["patient"] == 10_000 and s["rate_basis"] == "입원 식대"   # 5%(1,000) 아님


def test_count_zero_not_phantom_billed():
    """count=0(취소·영량) 라인은 0원 — `or 1`로 1건 유령청구되던 것 방지."""
    lines = [{"code": "AA", "unit_price": 30_000, "count": 0,
              "coverage": "NHIS", "encounter_type": "OUTPATIENT"}]
    inv = compute_invoice(lines)
    assert inv["total"] == 0 and inv["patient_share"] == 0
    # count 미지정은 1로 보정(회귀 가드)
    inv2 = compute_invoice([{"code": "AA", "unit_price": 30_000,
                             "coverage": "NHIS", "encounter_type": "OUTPATIENT"}])
    assert inv2["total"] == 30_000


def test_medical_aid_2_missing_rate_warns():
    """의료급여 2종 본인부담률 미설정(HIS copayRate·rates 둘 다 없음) → 경고(과소청구 가시화)."""
    lines = [{"code": "AA", "unit_price": 10_000, "count": 1,
              "coverage": "MEDICAL_AID", "medical_aid_class": "2"}]
    inv = compute_invoice(lines)
    assert any("의료급여 2종" in w for w in inv["warnings"])


def test_time_surcharge_utc_converted_to_kst():
    """시간외 가산 표시 — tz-aware(UTC) 진료시각을 KST 환산 후 야간/공휴 판정."""
    from datetime import datetime, timezone

    from erp.patient.billing_classify import time_surcharge
    # 21:00 KST(월) = 12:00 UTC(aware) → 야간
    assert "야간" in (time_surcharge(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)) or "")
    # 14:00 KST(월, 주간) = 05:00 UTC → 야간/공휴 아님
    assert time_surcharge(datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc)) is None
    # naive는 그대로(로컬 의미)
    assert "야간" in (time_surcharge(datetime(2026, 6, 1, 21, 0)) or "")


async def test_prepaid_scoped_by_care_month(session):
    """기수납 스코프 정합 — 6월 수납이 7월 청구서 기수납에 붙지 않음(교차월 과다상계 방지)."""
    from datetime import date

    from erp.patient.billing_service import BillingService
    p = Patient(chart_no="C-XM", name="환자", birth_date=date(1990, 1, 1))
    session.add(p)
    await session.flush()
    jun = Reception(patient_id=p.id, dept_code="IM", received_on=date(2026, 6, 10), source="HIS")
    jul = Reception(patient_id=p.id, dept_code="IM", received_on=date(2026, 7, 10), source="HIS")
    session.add_all([jun, jul])
    await session.flush()
    session.add_all([
        Payment(reception_id=jun.id, amount=50_000, method="CASH", status="PAID",
                paid_on=date(2026, 6, 10), care_month="2026-06"),
        Payment(reception_id=jul.id, amount=30_000, method="CASH", status="PAID",
                paid_on=date(2026, 7, 10), care_month="2026-07")])
    await session.flush()
    svc = BillingService()
    assert await svc._prepaid(session, chart_no="C-XM", care_month="2026-07") == 30_000  # 7월만
    assert await svc._prepaid(session, chart_no="C-XM", care_month="2026-06") == 50_000
    assert await svc._prepaid(session, chart_no="C-XM") == 80_000     # 스코프 없으면 전체


async def test_prepaid_nets_refunds_no_phantom(session):
    """전체검토 HIGH — 기수납 순액은 환불을 차감(환불 후 balance_due 유령 음수·반복환불 방지)."""
    from datetime import date

    from erp.patient.billing_service import BillingService
    from erp.patient.models import Refund
    p = Patient(chart_no="C-RF", name="환자", birth_date=date(1990, 1, 1))
    session.add(p)
    await session.flush()
    jul = Reception(patient_id=p.id, dept_code="IM", received_on=date(2026, 7, 10), source="HIS")
    session.add(jul)
    await session.flush()
    session.add(Payment(reception_id=jul.id, amount=100_000, method="CASH", status="PAID",
                        paid_on=date(2026, 7, 10), care_month="2026-07"))
    await session.flush()
    svc = BillingService()
    assert await svc._prepaid(session, chart_no="C-RF", care_month="2026-07") == 100_000
    # 과오납 2만 환불 — status는 그대로 PAID(환불은 별도 행)
    session.add(Refund(reception_id=jul.id, chart_no="C-RF", amount=20_000, method="CASH",
                       reason="과오납", refunded_on=date(2026, 7, 12), care_month="2026-07"))
    await session.flush()
    # 순액 = 100,000 − 20,000 = 80,000 (미차감이면 100,000 → 유령 환불 대상 지속)
    assert await svc._prepaid(session, chart_no="C-RF", care_month="2026-07") == 80_000
    assert await svc._prepaid(session, chart_no="C-RF") == 80_000     # since 스코프도 차감


def test_copay_round_down_10_won():
    """10원 절사(건보 시행령 §19) — 급여 본인부담 10원 미만 버림·절사분 보험자 흡수."""
    lines = [{"code": "AA", "unit_price": 12_340, "count": 1,
              "coverage": "NHIS", "encounter_type": "OUTPATIENT"}]   # 40% = 4,936 → 4,930
    inv = compute_invoice(lines, rates={"OUTPATIENT": 0.40})
    assert inv["patient_share"] == 4_930 and inv["patient_share_raw"] == 4_936
    assert inv["copay_rounding"] == 6
    assert inv["insurer_share"] == 12_340 - 4_930      # 절사분 보험자 부담
    assert inv["total"] == inv["patient_share"] + inv["insurer_share"]


def test_copay_round_excludes_noncovered_and_toggle():
    """비급여는 절사 제외(전액)·round_unit=1이면 절사 없음."""
    lines = [
        {"code": "G", "unit_price": 12_344, "count": 1, "coverage": "NHIS",
         "encounter_type": "OUTPATIENT"},                            # 급여 30% = 3,703(→3,700)
        {"code": "NC", "unit_price": 5_003, "count": 1, "coverage": "NONCOVERED",
         "encounter_type": "OUTPATIENT"}]                           # 비급여 5,003 전액(절사 X)
    inv = compute_invoice(lines, rates={"OUTPATIENT": 0.30})
    assert inv["patient_share"] == 3_700 + 5_003        # 급여만 절사, 비급여 전액
    assert inv["copay_rounding"] == 3
    raw = compute_invoice(lines, rates={"OUTPATIENT": 0.30}, round_unit=1)
    assert raw["patient_share"] == 3_703 + 5_003 and raw["copay_rounding"] == 0


def test_invoice_passes_review_basis_fields():
    """심사 판단 근거(연령·성별·산정특례·의료급여 종)를 라인에 통과시킨다.

    종전에는 `compute_invoice`의 화이트리스트가 이 값들을 떨어뜨려, 화면·사전심사에서
    **왜 이 수가가 인정/부인되는지**를 볼 수 없었다(연령 제한 수가·성별 제한 항목 판정 근거).
    개인정보 범위: HIS는 `patientAge`(정수)·`patientSex`만 보내고 **생년월일은 이 경로로
    들어오지 않는다** — 병원 개인정보보호책임자 확정(성별+연령·생년월일 미노출)과 일치.
    """
    out = compute_invoice([{
        "code": "AA100", "unit_price": 10000, "count": 1, "coverage": "NHIS",
        "encounter_type": "OUTPATIENT", "age": 5, "sex": "F",
        "special_case_type": "V193", "medical_aid_class": "1",
    }])
    ln = out["lines"][0]
    assert ln["age"] == 5 and ln["sex"] == "F"
    assert ln["special_case_type"] == "V193" and ln["medical_aid_class"] == "1"


def test_invoice_omits_birth_date_even_if_supplied():
    """생년월일 같은 미승인 필드는 통과시키지 않는다 — 화이트리스트의 본래 목적."""
    out = compute_invoice([{
        "code": "AA100", "unit_price": 10000, "count": 1, "coverage": "NHIS",
        "encounter_type": "OUTPATIENT", "birth_date": "1990-01-01", "rrn": "900101-1234567",
    }])
    ln = out["lines"][0]
    assert "birth_date" not in ln and "rrn" not in ln
