"""공시 등록 (K-IFRS 1024·1037·1010) — 특수관계자·우발·후속사건 등록·조회·충당전환."""
from datetime import date

from sqlalchemy import select

from erp.finance.disclosure_service import DisclosureService
from erp.finance.models import Account, Voucher, VoucherLine

svc = DisclosureService()


async def test_related_party_and_registry(session):
    p = await svc.add_related_party(session, name="홍길동", relation="이사", note="대표이사")
    await svc.add_rp_txn(session, party_id=p["id"], period="2026", txn_type="보수",
                         amount=120000000, balance=0)
    await svc.add_contingency(session, period="2026", kind="LIABILITY",
                              description="의료분쟁 소송 계류", estimate=50000000, likelihood="중간")
    await svc.add_subsequent_event(session, period="2026", event_date=date(2027, 2, 1),
                                   description="유형자산 취득 결의", adjusting=False)
    reg = await svc.registry(session, year=2026)
    assert reg["related_party_txns"][0]["party"] == "홍길동"
    assert reg["related_party_txns"][0]["amount"] == 120000000
    assert reg["contingencies"][0]["kind"] == "LIABILITY"
    assert reg["contingencies"][0]["estimate"] == 50000000
    assert reg["subsequent_events"][0]["adjusting"] is False
    # notes()에 registry 포함
    notes = await svc.notes(session, year=2026)
    assert "registry" in notes and notes["registry"]["contingencies"]


async def test_recognize_provision(session):
    session.add_all([
        Account(code="2130", name="기타충당부채", kind="LIABILITY"),
        Account(code="5680", name="충당부채전입액", kind="EXPENSE"),
    ])
    await session.flush()
    c = await svc.add_contingency(session, period="2026", kind="LIABILITY",
                                  description="손해배상 소송 — 패소 가능성 높음",
                                  estimate=30000000, likelihood="높음")
    out = await svc.recognize_provision(session, contingency_id=c["id"], on_date=date(2026, 12, 31))
    assert out["provision"] == 30000000 and out["voucher_id"]
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == f"contingency-provision:{c['id']}"))
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert ("5680", "DEBIT") in sides and ("2130", "CREDIT") in sides
    # status PROVISIONED + 멱등
    reg = await svc.registry(session, year=2026)
    assert reg["contingencies"][0]["status"] == "PROVISIONED"
    dup = await svc.recognize_provision(session, contingency_id=c["id"], on_date=date(2026, 12, 31))
    assert dup.get("skipped")
    # 추정액 없으면 거부
    c2 = await svc.add_contingency(session, period="2026", kind="LIABILITY",
                                   description="추정 곤란 분쟁", likelihood="중간")
    try:
        await svc.recognize_provision(session, contingency_id=c2["id"], on_date=date(2026, 12, 31))
        raise AssertionError("추정액 없는 우발이 인식됨")
    except Exception as e:  # noqa: BLE001
        assert "추정액" in str(e)


async def test_related_party_suggestions(session):
    p = await svc.add_related_party(session, name="홍길동", relation="이사")
    v = Voucher(voucher_on=date(2026, 5, 1), source_module="test", source_ref="rp1",
                memo="홍길동 이사 보수 지급", status="POSTED")
    session.add(v)
    await session.flush()
    session.add(VoucherLine(voucher_id=v.id, account_code="5320", side="DEBIT", amount=120000000))
    # 무관 전표(매칭 안 됨)
    v2 = Voucher(voucher_on=date(2026, 6, 1), source_module="test", source_ref="rp2",
                 memo="소모품 구매", status="POSTED")
    session.add(v2)
    await session.flush()
    session.add(VoucherLine(voucher_id=v2.id, account_code="5400", side="DEBIT", amount=500000))
    await session.flush()
    out = await svc.suggest_related_party_txns(session, year=2026)
    assert len(out["suggestions"]) == 1
    s = out["suggestions"][0]
    assert s["party"] == "홍길동" and s["count"] == 1 and s["amount"] == 120000000
    assert p["id"] == s["party_id"]


async def test_ifrs_activity(session):
    # 대손상각비(5690) 전기 → 1109 활동에 집계
    v = Voucher(voucher_on=date(2026, 6, 30), source_module="closing-ifrs",
                source_ref="allow:2026-06", memo="대손충당금 전입", status="POSTED")
    session.add(v)
    await session.flush()
    session.add_all([
        VoucherLine(voucher_id=v.id, account_code="5690", side="DEBIT", amount=4000000),
        VoucherLine(voucher_id=v.id, account_code="1190", side="CREDIT", amount=4000000),
    ])
    await session.flush()
    out = await svc.ifrs_activity(session, year=2026)
    assert out["activity"]["1109"]["amount"] == 4000000
    assert out["activity"]["1109"]["count"] == 1
    # 미해당 연도는 빈 집계
    assert "1109" not in (await svc.ifrs_activity(session, year=2025))["activity"]
    # 정합 카탈로그 — 20기준 + 활동 병합
    conf = await svc.ifrs_conformance(session, year=2026)
    assert conf["summary"]["standards"] == 20
    by_code = {i["code"]: i for i in conf["items"]}
    assert by_code["1109"]["activity"]["amount"] == 4000000
    assert by_code["1109"]["status"] == "✅" and by_code["1109"]["screen"] == "/finance-periods"
    assert by_code["1038"]["activity"] is None     # 활동 없는 기준은 None
    # 부합도는 하드코딩이 아니라 카탈로그에서 도출(✅=1·🟡=0.5) — 정직성(전체검토 Tier4)
    full = sum(1 for i in conf["items"] if i["status"] == "✅")
    partial = sum(1 for i in conf["items"] if i["status"] == "🟡")
    assert conf["summary"]["measured"] == full
    assert conf["summary"]["conformance"] == f"~{round((full + 0.5 * partial) / 20 * 100)}%"
