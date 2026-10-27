"""약품 코드 매핑 — 매칭 엔진·빌드·확정·invoice 자동변환."""
from datetime import date

from erp.patient.billing_service import BillingService
from erp.patient.models import BillingLine
from erp.regulatory.drug_map_service import DrugMapService
from erp.regulatory.drug_matcher import suggest
from erp.regulatory.models import FeeMaster


async def _seed_drug(session):
    session.add(FeeMaster(code="645302132", code_type="DRUG",
                          name="지스로맥스정250밀리그람(아지트로마이신)", unit_price=1075,
                          benefit_category="DRUG", valid_from=date(2026, 1, 1)))
    await session.flush()


async def test_suggest_brand_dose(session):
    await _seed_drug(session)
    out = await suggest(session, drug_name="지스로맥스정 250mg", ingredient="아지스로마이신")
    assert out["confidence"] == "HIGH" and out["insurance_code"] == "645302132"


async def test_build_confirm_and_invoice_mapping(session):
    await _seed_drug(session)
    # HIS 비표준코드 약품 라인(drug_name 동봉) — 미등재
    session.add(BillingLine(line_id="L1", chart_no="C-DM", code="M047803",
                            code_type="DRUG", count=2, coverage="NHIS",
                            encounter_type="OUTPATIENT", treated_on=date(2026, 5, 1),
                            drug_name="지스로맥스정 250mg", drug_ingredient="아지스로마이신",
                            unit_price_his=900))
    await session.flush()
    svc = DrugMapService()
    built = await svc.build_suggestions(session, actor=1)
    assert built["created"] == 1
    maps = await svc.list_maps(session, status="SUGGESTED")
    assert maps["total"] == 1 and maps["page"] == 1
    assert maps["items"][0]["his_code"] == "M047803" and maps["items"][0]["confidence"] == "HIGH"

    # 확정 전 — HIS 폴백(900)
    inv0 = await BillingService().invoice(session, chart_no="C-DM")
    assert inv0["lines"][0]["price_source"] == "HIS" and inv0["lines"][0]["unit_price"] == 900

    # 확정 → 이후 invoice는 보험코드 마스터 단가(1075)·price_source=MAP
    await svc.confirm(session, actor=1, his_code="M047803", insurance_code="645302132")
    inv1 = await BillingService().invoice(session, chart_no="C-DM")
    ln = inv1["lines"][0]
    assert ln["price_source"] == "MAP" and ln["unit_price"] == 1075
    assert ln["benefit_category"] == "DRUG"


async def test_confirm_rejects_nonexistent_insurance_code(session):
    session.add(BillingLine(line_id="L2", chart_no="C-DM2", code="M999",
                            code_type="DRUG", count=1, coverage="NHIS",
                            treated_on=date(2026, 5, 1), drug_name="미상약"))
    await session.flush()
    svc = DrugMapService()
    await svc.build_suggestions(session, actor=1)
    import pytest
    with pytest.raises(Exception):       # 약가 마스터 미존재 보험코드 거부
        await svc.confirm(session, actor=1, his_code="M999", insurance_code="000000000")


async def test_blood_products_excluded(session):
    """혈액제제는 혈액수가 체계 — 약가 매칭 비대상(미매칭·표기)."""
    out = await suggest(session, drug_name="농축적혈구(pRBC) 320mL", ingredient="적혈구")
    assert out["confidence"] is None and "혈액수가" in out["matched_name"]


async def test_form_disambiguation(session):
    """제형 일치 — '정' 요청에 현탁액/주사 후보 제외(오매칭 방지)."""
    from erp.regulatory.models import FeeMaster
    from datetime import date as _d
    session.add(FeeMaster(code="111", code_type="DRUG", name="타이레놀정500밀리그람",
                          unit_price=50, valid_from=_d(2026, 1, 1)))
    session.add(FeeMaster(code="222", code_type="DRUG", name="어린이타이레놀현탁액",
                          unit_price=28, valid_from=_d(2026, 1, 1)))
    await session.flush()
    out = await suggest(session, drug_name="타이레놀정 500mg", ingredient="아세트아미노펜")
    codes = {c["code"] for c in out["candidates"]}
    assert "111" in codes and "222" not in codes      # 현탁액 제외


async def test_confirm_rejects_form_mismatch(session):
    """H1: 제형 불일치 보험코드 확정 차단(정↔주사)."""
    from datetime import date as _d
    from erp.patient.models import BillingLine
    from erp.regulatory.models import FeeMaster
    import pytest
    session.add(FeeMaster(code="INJ1", code_type="DRUG", name="OO주사액500mg",
                          unit_price=100, valid_from=_d(2026, 1, 1)))
    session.add(BillingLine(line_id="LF", chart_no="CF", code="MTAB",
                            code_type="DRUG", count=1, coverage="NHIS",
                            treated_on=_d(2026, 5, 1), drug_name="OO정 500mg"))
    await session.flush()
    svc = DrugMapService()
    await svc.build_suggestions(session, actor=1)
    with pytest.raises(Exception):       # 정(PO_SOLID) ↔ 주사액(INJ) 차단
        await svc.confirm(session, actor=1, his_code="MTAB", insurance_code="INJ1")


async def test_insurance_product_code_master_direct(session):
    """HIS 동봉 보험코드(insuranceProductCode) → MASTER 직행(약제팀 확정 불필요)."""
    from datetime import date as _d
    from erp.patient.models import BillingLine
    from erp.regulatory.models import FeeMaster
    session.add(FeeMaster(code="643900480", code_type="DRUG", name="부루펜정400",
                          unit_price=152, benefit_category="DRUG", valid_from=_d(2026, 1, 1)))
    session.add(BillingLine(line_id="LI", chart_no="C-IPC", code="M040703",
                            code_type="DRUG", count=1, coverage="NHIS",
                            encounter_type="OUTPATIENT", treated_on=_d(2026, 6, 1),
                            insurance_product_code="643900480", unit_price_his=890))
    await session.flush()
    inv = await BillingService().invoice(session, chart_no="C-IPC")
    ln = inv["lines"][0]
    assert ln["price_source"] == "MASTER" and ln["unit_price"] == 152   # HIS 폴백 890 대체


async def test_brand_without_dose_caps_at_medium(session):
    """용량 미기재 브랜드 단일 후보 — 교차검증 불가라 HIGH 아닌 MEDIUM(자동확정 방지). F3 회귀."""
    session.add(FeeMaster(code="641900010", code_type="DRUG",
                          name="아스피린장용정100밀리그람", unit_price=30,
                          benefit_category="DRUG", valid_from=date(2026, 1, 1)))
    await session.flush()
    out = await suggest(session, drug_name="아스피린")           # 용량 표기 없음
    assert out["confidence"] == "MEDIUM"                          # 단일이라도 용량 미검증 → MEDIUM
    assert out["insurance_code"] is None                          # 자동확정 금지
    assert len(out["candidates"]) == 1
    # 용량 병기 시엔 여전히 HIGH(교차검증 성립)
    out2 = await suggest(session, drug_name="아스피린장용정 100mg")
    assert out2["confidence"] == "HIGH" and out2["insurance_code"] == "641900010"
