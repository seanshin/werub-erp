"""인증평가 조사항목 (상시준비·P3-2) — 등록·중복거부·상태갱신·요약 준비율·필터."""
import pytest

from erp.errors import ValidationError
from erp.regulatory.accreditation_service import AccreditationService

svc = AccreditationService()


async def _item(session, code, domain="CARE", status="GAP"):
    return await svc.create(session, domain=domain, item_code=code, title=f"항목{code}",
                            status=status, actor=1)


async def test_create_and_duplicate_rejected(session):
    await _item(session, "3.1.1")
    with pytest.raises(ValidationError):
        await _item(session, "3.1.1")               # 동일 standard+item_code 중복


async def test_invalid_domain_status(session):
    with pytest.raises(ValidationError):
        await svc.create(session, domain="BAD", item_code="x", title="t")
    with pytest.raises(ValidationError):
        await svc.create(session, domain="CARE", item_code="y", title="t", status="BAD")


async def test_update_status_sets_reviewed(session):
    it = await _item(session, "3.1.2", status="GAP")
    out = await svc.update(session, item_id=it.id, status="READY", evidence_note="비치 완료")
    assert out.status == "READY" and out.last_reviewed_on is not None


async def test_summary_ready_rate_excludes_na(session):
    await _item(session, "C1", domain="CARE", status="READY")
    await _item(session, "C2", domain="CARE", status="GAP")
    await _item(session, "C3", domain="CARE", status="NA")     # NA는 분모 제외
    s = await svc.summary(session)
    care = next(d for d in s["domains"] if d["domain"] == "CARE")
    assert care["READY"] == 1 and care["GAP"] == 1 and care["NA"] == 1
    assert care["ready_rate"] == 50.0                          # 1/(1+1) — NA 제외
    assert s["overall_ready_rate"] == 50.0


async def test_list_filters(session):
    await _item(session, "B1", domain="BASIC", status="READY")
    await _item(session, "A1", domain="ADMIN", status="GAP")
    assert len(await svc.list_items(session, domain="BASIC")) == 1
    assert len(await svc.list_items(session, status="GAP")) == 1
    assert all(x["domain"] == "ADMIN" for x in await svc.list_items(session, domain="ADMIN"))


async def test_summary_empty_domain_none_rate(session):
    s = await svc.summary(session)
    # 항목 없는 영역은 준비율 None(0 나눗셈 방지)
    assert all(d["ready_rate"] is None for d in s["domains"])
    assert s["overall_ready_rate"] is None and s["total_items"] == 0
