"""영업부문 보고 (K-IFRS 1108) — 진료과별 수익·손익·자산·보고부문 임계."""
from datetime import date

from erp.analytics.service import AnalyticsService
from erp.common.models import Department
from erp.finance.models import FixedAsset

svc = AnalyticsService()


async def test_segment_report(session):
    from erp.claims.models import ClaimBatch, ClaimStatement
    session.add_all([Department(id=5, name="내과", code="IM"),
                     Department(id=6, name="정형외과", code="OS")])
    batch = ClaimBatch(care_month="2026-05", receipt_no="SG-1", notice_arrived_on=date(2026, 6, 1))
    session.add(batch)
    await session.flush()
    session.add_all([
        ClaimStatement(batch_id=batch.id, stmt_serial="1", dept_code="IM",
                       claimed_amt=10000000, decided_amt=10000000, adjusted_amt=0),
        ClaimStatement(batch_id=batch.id, stmt_serial="2", dept_code="OS",
                       claimed_amt=1000000, decided_amt=1000000, adjusted_amt=0),
    ])
    session.add(FixedAsset(asset_no="A1", name="CT", category="MEDICAL", acquired_on=date(2025, 1, 1),
                           acquisition_cost=50000000, useful_life_years=5, department_id=5,
                           accumulated_depreciation=10000000))
    await session.flush()
    out = await svc.segment_report(session, yyyymm="2026-05")
    im = next(s for s in out["segments"] if s["dept_code"] == "IM")
    assert im["revenue"] == 10000000
    assert im["assets"] == 40000000          # 5천만 − 1천만 감가
    assert im["reportable"] is True          # 수익·자산 모두 10%+
    os_seg = next(s for s in out["segments"] if s["dept_code"] == "OS")
    assert os_seg["reportable"] is False     # 수익 100만 < 10% of 1,100만
    assert out["total_revenue"] == 11000000
