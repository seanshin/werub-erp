"""HIS 미청구 진행분 pull → 미청구수익 발생 (K-IFRS 1115 B-1). HIS _pull monkeypatch."""
from datetime import date

import erp.integrations.his_billing as hb
from erp.finance.models import Account, Voucher, VoucherLine
from sqlalchemy import select


async def _accts(session):
    session.add_all([
        Account(code="1135", name="계약자산(미청구수익)", kind="ASSET"),
        Account(code="4110", name="의료수익", kind="REVENUE"),
    ])
    await session.flush()


async def test_backfill_lines_uses_pii_read_key(session, monkeypatch):
    """billing/lines(환자 PII)는 his_pii_read_key로 호출, 미설정 시 webhook_key 폴백(S-1)."""
    from erp.config import get_settings
    s = get_settings()
    old_wk, old_pii = s.his_webhook_key, s.his_pii_read_key
    seen: dict = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": []}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            seen["key"] = headers["X-Integration-Key"]
            return _Resp()

    monkeypatch.setattr(hb.httpx, "AsyncClient", _Client)
    try:
        s.his_webhook_key, s.his_pii_read_key = "wk", "pii_k"
        await hb.backfill_lines(session, from_on=date(2026, 6, 1), to_on=date(2026, 6, 30))
        assert seen["key"] == "pii_k"                      # PII 전용키 우선
        s.his_pii_read_key = None
        await hb.backfill_lines(session, from_on=date(2026, 6, 1), to_on=date(2026, 6, 30))
        assert seen["key"] == "wk"                         # 미설정 → webhook_key 폴백(무중단)
    finally:
        s.his_webhook_key, s.his_pii_read_key = old_wk, old_pii


async def test_pull_aggregates_and_accrues(session, monkeypatch):
    await _accts(session)
    rows = [
        # 외래 당일 종결: HIS unbilledAmount 1,000,000 직접
        {"deptCode": "내과", "progressBasis": "SAME_DAY", "incurredAmount": 1000000,
         "billedAmount": 0, "unbilledAmount": 1000000, "status": "DISCHARGED_UNBILLED"},
        # 입원 FFS: incurred 3,000,000 − billed 500,000 = 2,500,000 (unbilledAmount 제공)
        {"deptCode": "정형외과", "progressBasis": "ACTUAL_LINES", "incurredAmount": 3000000,
         "billedAmount": 500000, "unbilledAmount": 2500000, "status": "IN_PROGRESS"},
        # 입원 DRG 정액: unbilledAmount 미제공 → drg 4,000,000 × 5/10일 = 2,000,000
        {"deptCode": "외과", "progressBasis": "LOS_RATIO", "drgAmount": 4000000,
         "daysAdmitted": 5, "standardDays": 10, "billedAmount": 0, "status": "IN_PROGRESS"},
        # 이미 청구 완료 → 제외
        {"deptCode": "내과", "progressBasis": "SAME_DAY", "incurredAmount": 700000,
         "billedAmount": 700000, "unbilledAmount": 0, "status": "DISCHARGED_UNBILLED"},
    ]

    async def fake_pull(path, params=None):
        assert "unbilled-progress" in path
        return rows
    monkeypatch.setattr(hb, "_pull", fake_pull)

    out = await hb.pull_unbilled_progress(session, as_of=date(2026, 6, 30))
    # 100만 + 250만 + 200만 = 550만 (4번째 제외)
    assert out["unbilled_total"] == 5500000 and out["counted"] == 3 and out["skipped"] == 1
    assert out["accrual"]["amount"] == 5500000
    # 미청구수익 발생 전표(1135/4110)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "unbilled:2026-06"))
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()}
    assert ("1135", "DEBIT") in sides and ("4110", "CREDIT") in sides
