"""경영진 종합 대시보드 — overview + 자금·재고·자산·정합 키."""
from erp.analytics.service import AnalyticsService

svc = AnalyticsService()


async def test_executive_aggregates_all_domains(session):
    out = await svc.executive(session, yyyymm="2026-05")
    # overview 도메인 + 신규 도메인 키 존재 (재무비율·검진권 추가 반영)
    for k in ["재무", "인사", "진료", "보험", "자금", "재고", "자산", "재무비율", "검진권",
              "고정비용", "변동비용", "정합"]:
        assert k in out, f"누락 키: {k}"
    assert {"고정비합계", "인건비", "감가상각", "고정비율_%"} <= set(out["고정비용"])
    assert {"변동비합계", "재료비", "변동비율_%"} <= set(out["변동비용"])
    assert "6개월_최저잔액" in out["자금"]
    assert "재고자산평가" in out["재고"] and "장기재고건수" in out["재고"]
    assert "장부가총계" in out["자산"]
    assert {"유동비율", "부채비율_%", "의료이익률_%", "인건비율_%"} <= set(out["재무비율"])
    assert {"발급액", "사용액", "미사용잔액", "미완료콜백"} <= set(out["검진권"])
    assert "재무상태표_균형" in out["정합"]
