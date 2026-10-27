"""의약품 표준코드 마스터 — 로더 파싱·매처 표준코드 폴백·워크스테이션 조회 EP."""
import importlib.util
from datetime import date
from pathlib import Path

import pytest

from erp.common.router import CurrentUser
from erp.errors import ValidationError
from erp.regulatory.drug_matcher import suggest
from erp.regulatory.models import DrugStdCode, FeeMaster
from erp.regulatory.router import lookup_drug_std

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=1, license_flag=True)

_spec = importlib.util.spec_from_file_location(
    "import_drug_std_codes",
    Path(__file__).parents[1] / "scripts" / "import_drug_std_codes.py")
_loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_loader)


def test_loader_parse_rows(tmp_path):
    """CP949 파싱 — 표준코드 dedupe(마지막 우선)·공백 strip·취소일·수량·무코드 스킵."""
    header = ",".join(["한글상품명", "업체명", "약품규격", "제품총수량", "제형구분", "포장형태",
                       "품목기준코드", "품목허가일자", "전문일반구분", "대표코드", "표준코드",
                       "제품코드(개정후)", "일반명코드(성분명코드)", "비고", "취소일자",
                       "양도양수적용(공고)일자", "양도양수종료일자", "일련번호생략여부",
                       "일련번호생략사유", "국제표준코드(ATC코드)", "특수관리약품구분",
                       "의약품판독장비구분"])
    rows = [
        # 정상 — 코드 뒤 공백(원본 그대로) strip 확인
        "타이레놀정500mg,한국얀센,500mg,30,정제,병,200000001 ,2000-01-01,전문,"
        "8806400000000 ,8806400000001 ,644500010 ,100502ATB,,,,,N,,N02BE01,해당사항없음,바코드",
        # 동일 표준코드 재수록 — 마지막 행 우선(회사명 갱신)
        "타이레놀정500mg,한국얀센(주),500mg,30,정제,병,200000001 ,2000-01-01,전문,"
        "8806400000000 ,8806400000001 ,644500010 ,100502ATB,,2025-06-01,,,N,,N02BE01,해당사항없음,바코드",
        # 표준코드 없음 → 스킵
        "무코드약품,회사,10mg,1,정제,병,200000002 ,2000-01-01,전문,, ,644500011 ,,,,,,N,,,해당없음,바코드",
    ]
    p = tmp_path / "master.csv"
    p.write_bytes(("\n".join([header, *rows]) + "\n").encode("cp949"))
    out = _loader.parse_rows(str(p), source_file="master.csv")
    assert len(out) == 1                                   # dedupe + 무코드 스킵
    r = out["8806400000001"]
    assert r["name"] == "타이레놀정500mg" and r["company"] == "한국얀센(주)"   # 마지막 행 우선
    assert r["item_seq"] == "200000001" and r["product_code"] == "644500010"  # 공백 strip
    assert r["ingredient_code"] == "100502ATB" and r["pack_qty"] == 30
    assert r["canceled_on"] == date(2025, 6, 1) and r["atc"] == "N02BE01"


async def test_matcher_std_code_bridge_fallback(session):
    """표준코드 폴백 — fee_master 표기와 다른 상품명을 표준코드 마스터로 브리지(MEDIUM 상한)."""
    # fee_master 등재명은 "아세트아미노펜정500밀리그램" — HIS 명칭 "타이레놀..."과 ILIKE 미매칭
    session.add(FeeMaster(code="644500010", code_type="DRUG",
                          name="아세트아미노펜정500밀리그램", unit_price=51,
                          valid_from=date(2026, 1, 1)))
    session.add(DrugStdCode(std_code="8806400000001", item_seq="200000001",
                            product_code="644500010", name="타이레놀정500mg",
                            company="한국얀센"))
    await session.flush()
    out = await suggest(session, drug_name="타이레놀정 500mg")
    assert out["confidence"] == "MEDIUM"                   # 브리지는 단일 후보라도 자동확정 금지
    assert out["candidates"][0]["code"] == "644500010"
    assert out["candidates"][0]["by_std_code"] is True
    assert out["insurance_code"] is None                   # MEDIUM → 약제팀 확인 필요


async def test_matcher_std_bridge_excludes_canceled_and_unpriced(session):
    """취소 품목·약가 미등재(청구 불가) 표준코드는 후보 제외."""
    session.add(DrugStdCode(std_code="8806400000002", product_code="644500099",
                            name="구약품정 100mg", canceled_on=date(2024, 1, 1)))
    session.add(DrugStdCode(std_code="8806400000003", product_code="644500098",
                            name="신약품정 100mg"))     # fee_master 미등재
    await session.flush()
    out = await suggest(session, drug_name="구약품정 100mg")
    assert out["confidence"] is None                       # 취소 → 미매칭
    out2 = await suggest(session, drug_name="신약품정 100mg")
    assert out2["confidence"] is None                      # 약가 미등재 → 후보 아님


async def test_drug_std_lookup_ep(session):
    """워크스테이션 조회 — 바코드/제품코드 정확일치 우선, 그 외 상품명 검색·짧은 질의 거부."""
    session.add(DrugStdCode(std_code="8806400000001", product_code="644500010",
                            name="타이레놀정500mg", company="한국얀센", rx_otc="전문",
                            special_class="해당사항없음"))
    await session.flush()
    exact = await lookup_drug_std(q="8806400000001", user=ADMIN, session=session)
    assert exact["match"] == "exact" and exact["items"][0]["product_code"] == "644500010"
    by_code = await lookup_drug_std(q="644500010", user=ADMIN, session=session)
    assert by_code["match"] == "exact" and by_code["count"] == 1
    by_name = await lookup_drug_std(q="타이레놀", user=ADMIN, session=session)
    assert by_name["match"] == "name" and by_name["count"] == 1
    with pytest.raises(ValidationError, match="2자"):
        await lookup_drug_std(q="8", user=ADMIN, session=session)


async def test_ai_review_context_enriched_with_std_master(session, monkeypatch):
    """AI 종합검토 컨텍스트 보강 — 약가코드 적발에 성분코드·ATC·전문일반 투입(결정적 보조)."""
    from types import SimpleNamespace

    from erp.claims.pre_review import PreReviewEngine
    from erp.claims.router import case_ai_review
    from erp.claims.worklist_service import WorklistService
    from erp.config import get_settings
    from erp.integrations import weve_rag

    session.add(DrugStdCode(std_code="8806400000001", product_code="644500010",
                            name="타이레놀정500mg", ingredient_code="100502ATB",
                            atc="N02BE01", rx_otc="전문의약품"))
    await session.flush()
    lines = [{"chart_no": "C-AI", "code": "644500010", "count": 1, "amount": 51,
              "treated_on": date(2026, 6, 5)},
             {"chart_no": "C-AI", "code": "644500010", "count": 1, "amount": 51,
              "treated_on": date(2026, 6, 5)}]
    rule = SimpleNamespace(code="R-DUP", name="중복", rule_type="DUPLICATE",
                           params={}, severity="BLOCK", active=True)
    findings = PreReviewEngine().evaluate(lines=lines, rules=[rule], fee={})
    out = await WorklistService().save_run(session, actor=1, lines=lines, findings=findings)
    cid = out["cases"][0]["case_id"]

    captured: list[dict] = []

    class _FakeClient:
        def __init__(self, *a, **k): ...
        def diagnose(self, **kw):
            captured.append(kw)
            return {"has_evidence": True, "risk": "LOW"}

    monkeypatch.setattr(weve_rag, "WeveRagClient", _FakeClient)
    monkeypatch.setattr(get_settings(), "weve_ai_api_key", "sk-test")
    r = await case_ai_review(case_id=cid, user=ADMIN, session=session)
    assert r["available"] is True and captured
    ctx = captured[0].get("context") or ""
    assert "성분코드 100502ATB" in ctx and "ATC N02BE01" in ctx and "전문의약품" in ctx
