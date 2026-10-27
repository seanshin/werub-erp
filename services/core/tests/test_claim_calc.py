"""청구 산정 엔진(D3-B1) + 격리 EDI 매핑(D3-B2 시드) 테스트.

계산식 검증: as-of 단가·종별가산(TARIFF만)·묶음 제외·본인부담 분리·소급 재계산 안정.
EDI 매핑: 버전 레지스트리·시드 마커·제출 미연결 가드.
"""
from datetime import date

import pytest

from erp.claims.claim_calc import ClaimCalcEngine, ClaimLine
from erp.claims.edi_mapping import EDI_PRODUCTION_READY, map_to_edi
from erp.claims.models import BundlingRule
from erp.regulatory.models import (
    FeeMaster, InstitutionGradeSetting, InstitutionSurchargeRate,
)

engine = ClaimCalcEngine()


async def _seed_fees(session):
    session.add_all([
        # 진찰료 v1(구)·v2(신) — 소급 재계산 확인용
        FeeMaster(code="AA100", code_type="TARIFF", name="진찰료", unit_price=15000,
                  valid_from=date(2026, 1, 1), valid_to=date(2026, 7, 1)),
        FeeMaster(code="AA100", code_type="TARIFF", name="진찰료", unit_price=15800,
                  valid_from=date(2026, 7, 1), valid_to=None),
        # 패키지 수가 + 세부(묶음 제외 대상)
        FeeMaster(code="PKG10", code_type="TARIFF", name="처치팩", unit_price=30000,
                  valid_from=date(2026, 1, 1), valid_to=None),
        FeeMaster(code="SUB10", code_type="TARIFF", name="세부처치", unit_price=5000,
                  valid_from=date(2026, 1, 1), valid_to=None),
        # 비급여
        FeeMaster(code="NC100", code_type="NONCOVERED", name="상급병실료", unit_price=50000,
                  benefit_type="비급여", valid_from=date(2026, 1, 1), valid_to=None),
    ])
    await session.flush()


async def test_basic_split_outpatient(session):
    """외래 급여 — 본인부담률(병원 40%) 적용·공단부담=총액−본인부담."""
    await _seed_fees(session)
    r = await engine.calc_statement(
        session, treated_on=date(2026, 7, 10), grade="HOSPITAL",
        lines=[ClaimLine(code="AA100", count=1)])
    assert r["total"] == 15800
    # 40% = 6320 → 10원 절사 6320
    assert r["patient_share"] == 6320
    assert r["claim_amount"] == 15800 - 6320
    assert r["non_covered"] == 0


async def test_grade_copay_rate_from_settings(session):
    """종별 본인부담률 — InstitutionGradeSetting(의원 30%)이 실제 본인부담 분리에 반영(병원 40% 아님)."""
    await _seed_fees(session)
    session.add(InstitutionGradeSetting(grade="CLINIC", outpatient_rate=0.30,
                                        effective_from=date(2026, 1, 1)))
    await session.flush()
    r = await engine.calc_statement(   # grade 미지정 → 설정에서 CLINIC 자체 해소
        session, treated_on=date(2026, 7, 10),
        lines=[ClaimLine(code="AA100", count=1)])
    assert r["grade"] == "CLINIC"
    assert r["patient_share"] == 4740     # 15800 × 30% = 4740 (병원 40%=6320 아님)
    assert r["claim_amount"] == 15800 - 4740


async def test_as_of_retroactive(session):
    """소급 재계산 — 과거 진료일은 당시 단가(15000)로 산정(시간 역행 안정)."""
    await _seed_fees(session)
    r = await engine.calc_statement(
        session, treated_on=date(2026, 3, 15), grade="HOSPITAL",
        lines=[ClaimLine(code="AA100", count=1)])
    assert r["total"] == 15000       # 신 버전(15800) 아닌 당시 단가


async def test_surcharge_applies_to_tariff_only(session):
    """종별가산은 행위료(TARIFF)에만 — 비급여는 미적용."""
    await _seed_fees(session)
    session.add(InstitutionSurchargeRate(grade="TERTIARY", rate=0.30, applies_to="TARIFF"))
    await session.flush()
    r = await engine.calc_statement(
        session, treated_on=date(2026, 7, 10), grade="TERTIARY",
        lines=[ClaimLine(code="AA100", count=1),
               ClaimLine(code="NC100", code_type="NONCOVERED", coverage="NONCOVERED", count=1)])
    line_aa = next(x for x in r["lines"] if x["code"] == "AA100")
    line_nc = next(x for x in r["lines"] if x["code"] == "NC100")
    assert line_aa["unit_price"] == round(15800 * 1.30)   # 20540 가산
    assert line_nc["unit_price"] == 50000                 # 비급여 무가산
    assert r["non_covered"] == 50000


async def test_noncovered_tariff_gets_no_surcharge(session):
    """비급여인 행위(code_type=TARIFF·benefit_type=비급여)에는 종별가산 미적용(급여 행위료 개념)."""
    session.add_all([
        FeeMaster(code="TB100", code_type="TARIFF", name="비급여행위", unit_price=10000,
                  benefit_type="비급여", valid_from=date(2026, 1, 1), valid_to=None),
        InstitutionSurchargeRate(grade="TERTIARY", rate=0.30, applies_to="TARIFF"),
    ])
    await session.flush()
    r = await engine.calc_statement(
        session, treated_on=date(2026, 7, 10), grade="TERTIARY",
        lines=[ClaimLine(code="TB100", count=1)])
    line = r["lines"][0]
    assert line["unit_price"] == 10000       # 가산 없음
    assert r["non_covered"] == 10000 and r["claim_amount"] == 0


async def test_bundling_excludes_child(session):
    """묶음 — 패키지(PKG10) 존재 시 세부(SUB10) 청구 제외."""
    await _seed_fees(session)
    session.add(BundlingRule(parent_code="PKG10", child_codes="SUB10", rule_type="BUNDLE",
                             name="처치팩 묶음"))
    await session.flush()
    r = await engine.calc_statement(
        session, treated_on=date(2026, 7, 10), grade="HOSPITAL",
        lines=[ClaimLine(code="PKG10", count=1), ClaimLine(code="SUB10", count=1)])
    assert "SUB10" in r["bundled_excluded"]
    assert r["total"] == 30000       # 세부(5000) 미포함


async def test_unresolved_code_flagged(session):
    """미등록 코드 — unresolved 경고·0원(합계 미반영)."""
    await _seed_fees(session)
    r = await engine.calc_statement(
        session, treated_on=date(2026, 7, 10), grade="HOSPITAL",
        lines=[ClaimLine(code="ZZZ99", count=1)])
    assert "ZZZ99" in r["unresolved_codes"]
    assert r["total"] == 0


async def test_edi_mapping_seed_marker(session):
    """EDI 매핑 — 시드 마커·제출 미연결 가드."""
    await _seed_fees(session)
    calc = await engine.calc_statement(
        session, treated_on=date(2026, 7, 10), grade="HOSPITAL",
        lines=[ClaimLine(code="AA100", count=1)])
    edi = map_to_edi(calc, provider_code="12345678")
    assert edi["_seed"] is True
    assert edi["_production_ready"] is EDI_PRODUCTION_READY is False
    assert edi["mapping_version"] == "SEED_v0"
    assert edi["HEAD"]["provider_code"] == "12345678"
    assert edi["AMT"]["total"] == 15800


def test_edi_unknown_version_raises():
    """미등록 매핑 버전 — ValueError(오포맷 방지)."""
    with pytest.raises(ValueError):
        map_to_edi({"lines": []}, version="NOPE_v9")


def test_edi_temporal_version_resolution():
    """발효일 기반 시점 해소 — 소급 재계산 시 당시 포맷 버전 선택(시간 역행)."""
    from erp.claims.edi_mapping import EdiMappingVersion, _pick
    reg = [
        EdiMappingVersion("F2024", date(2024, 1, 1), True, "2024 포맷", lambda c, **k: {}),
        EdiMappingVersion("F2026", date(2026, 1, 1), True, "2026 포맷", lambda c, **k: {}),
    ]
    assert _pick(reg, date(2025, 6, 1)).version == "F2024"   # 2025 청구 → 2024 포맷
    assert _pick(reg, date(2026, 3, 1)).version == "F2026"   # 2026 청구 → 2026 포맷
    assert _pick(reg, date(2020, 1, 1)).version == "F2024"   # 최초 이전 → 최초 폴백


async def test_edi_as_of_picks_era_format(session):
    """map_to_edi가 산정 treated_on으로 당시 포맷 자동 선택(현행 단일 버전=SEED_v0)."""
    await _seed_fees(session)
    calc = await engine.calc_statement(
        session, treated_on=date(2026, 3, 15), grade="HOSPITAL",
        lines=[ClaimLine(code="AA100", count=1)])
    edi = map_to_edi(calc)                      # as_of 미지정 → calc.treated_on(2026-03-15) 사용
    assert edi["mapping_version"] == "SEED_v0"
    assert edi["mapping_effective_from"] == "2000-01-01"


async def test_calc_for_chart_reads_billing_lines(session):
    """실 진료내역(BillingLine)으로 산정 — 라인별 진료일 as-of·취소 제외·care_month 범위."""
    from erp.patient.models import BillingLine
    await _seed_fees(session)   # AA100 v1(6/30까지 15000)·v2(7/1~ 15800)
    session.add_all([
        BillingLine(line_id="L1", chart_no="C-1", code="AA100", code_type="TARIFF", count=1,
                    coverage="NHIS", encounter_type="OUTPATIENT", treated_on=date(2026, 6, 20)),
        BillingLine(line_id="L2", chart_no="C-1", code="AA100", code_type="TARIFF", count=1,
                    coverage="NHIS", encounter_type="OUTPATIENT", treated_on=date(2026, 7, 5)),
        # 취소 라인 — 집계 제외
        BillingLine(line_id="L3", chart_no="C-1", code="AA100", code_type="TARIFF", count=1,
                    coverage="NHIS", encounter_type="OUTPATIENT", treated_on=date(2026, 7, 6),
                    cancelled=True),
    ])
    await session.flush()
    r = await engine.calc_for_chart(session, chart_no="C-1")
    # L1(6/20)=당시 15000 + L2(7/5)=15800 = 30800 (라인별 as-of), 취소 L3 제외
    assert r["total"] == 30800
    assert r["chart_no"] == "C-1"
