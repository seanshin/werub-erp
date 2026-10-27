"""회계정책·추정변경·오류 (K-IFRS 1008) — 등록·소급/전진·registry 반영."""
from erp.finance.disclosure_service import DisclosureService

svc = DisclosureService()


async def test_accounting_change_registry(session):
    await svc.add_accounting_change(session, period="2026", change_type="POLICY",
                                    description="재고 원가흐름 FIFO→가중평균 변경", treatment="RETROSPECTIVE",
                                    standard="K-IFRS 1002", effect_amount=12000000)
    await svc.add_accounting_change(session, period="2026", change_type="ESTIMATE",
                                    description="감가 내용연수 변경", treatment="PROSPECTIVE")
    await svc.add_accounting_change(session, period="2026", change_type="ERROR",
                                    description="전기 비용 누락 수정", treatment="RETROSPECTIVE",
                                    effect_amount=3000000)
    reg = await svc.registry(session, year=2026)
    chgs = reg["accounting_changes"]
    assert len(chgs) == 3
    pol = next(c for c in chgs if c["change_type"] == "POLICY")
    assert pol["treatment"] == "RETROSPECTIVE" and pol["effect_amount"] == 12000000
    est = next(c for c in chgs if c["change_type"] == "ESTIMATE")
    assert est["treatment"] == "PROSPECTIVE"
    # notes()에도 포함
    notes = await svc.notes(session, year=2026)
    assert notes["registry"]["accounting_changes"]
