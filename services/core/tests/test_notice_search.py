"""공시 전문검색 (AI 사전심사 P0 retrieval) — 키워드·temporal·스니펫."""
from datetime import date

from erp.regulatory.notice_search import NoticeSearchService, make_snippet
from erp.regulatory.service import NoticeService

svc = NoticeService()
search = NoticeSearchService()


def test_make_snippet():
    text = "가나다 " * 20 + "동일 진료일 중복 산정 불가 " + "라마바 " * 20
    snip = make_snippet(text, ["중복", "산정"], width=60)
    assert "중복" in snip and snip.startswith("…") and snip.endswith("…")


async def _seed(session):
    await svc.create(session, notice_type="REVIEW_CRITERIA", title="외래 산정기준",
                     body_text="동일 진료일 동일 행위 중복 산정 불가. AA001은 1일 1회만 인정.",
                     effective_from=date(2026, 1, 1), effective_to=None, created_by=1)
    await svc.create(session, notice_type="TARIFF", title="수가 개정 안내",
                     body_text="BB002 단가 인상 시행.", effective_from=date(2026, 1, 1),
                     effective_to=None, created_by=1)
    await session.flush()


async def test_search_keyword_match_and_snippet(session):
    await _seed(session)
    out = await search.search(session, q="중복 산정", as_of=date(2026, 5, 1))
    assert out and out[0]["notice_type"] == "REVIEW_CRITERIA"
    assert out[0]["score"] == 2 and "중복" in out[0]["snippet"]


async def test_search_type_filter(session):
    await _seed(session)
    out = await search.search(session, q="단가", notice_type="REVIEW_CRITERIA")
    assert out == []                               # 단가는 TARIFF에만 → REVIEW_CRITERIA 필터로 0
    out2 = await search.search(session, q="단가", notice_type="TARIFF")
    assert len(out2) == 1


async def test_search_temporal(session):
    await _seed(session)
    # 시행일(2026-01-01) 이전 진료일 → 유효 공시 없음
    assert await search.search(session, q="중복", as_of=date(2025, 12, 1)) == []
    assert await search.search(session, q="중복", as_of=date(2026, 6, 1))


async def test_search_empty_query(session):
    await _seed(session)
    assert await search.search(session, q="   ") == []
