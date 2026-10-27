"""weve RAG 검색 클라이언트 — SSRF·응답 파싱."""
import httpx
import pytest

from erp.integrations.weve_rag import WeveRagClient


def test_ssrf_blocks_non_weve():
    with pytest.raises(ValueError, match="허용되지"):
        WeveRagClient("https://evil.example.com", "sk-x")


def test_search_parses_results():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer sk-x"
        assert "/api/notice/search" in request.url.path
        return httpx.Response(200, json={"results": [
            {"id": "ntc-1", "text": "동일 진료일 중복 산정 불가", "score": 0.9,
             "metadata": {"title": "외래 산정기준", "notice_type": "REVIEW_CRITERIA"}}]})
    client = WeveRagClient("https://example.org/ollama", "sk-x",
                           transport=httpx.MockTransport(handler))
    rows = client.search("중복 산정", n_results=3)
    assert len(rows) == 1 and rows[0]["metadata"]["title"] == "외래 산정기준"


def test_diagnose_parses():
    def handler(request):
        assert "/api/notice/diagnose" in request.url.path
        body = httpx.Response  # noqa
        return httpx.Response(200, json={"data": {"risk_level": "HIGH", "risk_score": 0.9,
            "evidence_citations": [{"title": "외래 재진 진찰료 산정기준"}],
            "recommendation": "재검토", "has_evidence": True}})
    c = WeveRagClient("https://example.org/ollama", "sk-x", transport=httpx.MockTransport(handler))
    out = c.diagnose(code="AA157", context="동일일 재진 2회", notice_type=["REVIEW_CRITERIA"])
    assert out["risk_level"] == "HIGH" and out["has_evidence"] is True
