"""원가 배부 스냅샷·진료과 손익."""
from erp.costing.service import CostSnapshotService

svc = CostSnapshotService()


async def test_snapshot_save_and_read(session, monkeypatch):
    # allocate 결과를 스텁(부서 2개)
    async def fake_allocate(self, session, *, yyyymm, material_amount=0, overhead_amount=0):
        # 실 엔진과 동일하게 grand_total·pools(입력 원가풀) 포함 — 정합 검증 대상
        return {"yyyymm": yyyymm, "grand_total": 1000,
                "pools": [{"name": "인건비", "amount": 800, "driver": "HEADCOUNT"},
                          {"name": "재료비", "amount": 200, "driver": "CASES"}],
                "objects": [
                    {"object_id": 5, "department": "내과", "total": 700,
                     "pools": {"인건비": 500, "재료비": 200}},
                    {"object_id": 6, "department": "정형외과", "total": 300, "pools": {"인건비": 300}}]}
    from erp.costing.abc_service import AbcAllocationService
    monkeypatch.setattr(AbcAllocationService, "allocate", fake_allocate)
    out = await svc.save_snapshot(session, yyyymm="2026-05", material_amount=200)
    await session.flush()
    assert out["departments"] == 2 and out["total_allocated"] == 1000 and out["reconciled"]
    rows = await svc.snapshots(session, yyyymm="2026-05")
    assert rows[0]["total_cost"] == 700 and rows[0]["material"] == 200


async def test_snapshot_reconciled_false_on_dropped_pool(session, monkeypatch):
    """심층 재검토 HIGH — 동인 총가중치 0인 풀이 드롭돼 배부액<입력 풀 합이면 reconciled=False.

    과거 comp_total이 total과 동일 식이라 항상 True(가짜 검증)였다."""
    async def fake_allocate(self, session, *, yyyymm, material_amount=0, overhead_amount=0):
        # 입력 풀 인건비 800+재료비 200=1000이나, 재료비 풀이 드롭돼 배부액은 800만
        return {"yyyymm": yyyymm, "grand_total": 800,
                "pools": [{"name": "인건비", "amount": 800, "driver": "HEADCOUNT"},
                          {"name": "재료비", "amount": 200, "driver": "CASES"}],
                "objects": [{"object_id": 5, "department": "내과", "total": 800,
                             "pools": {"인건비": 800}}]}
    from erp.costing.abc_service import AbcAllocationService
    monkeypatch.setattr(AbcAllocationService, "allocate", fake_allocate)
    out = await svc.save_snapshot(session, yyyymm="2026-06", material_amount=200)
    assert out["input_total"] == 1000 and out["total_allocated"] == 800
    assert out["reconciled"] is False           # 200 누락 검출(과거엔 True로 은폐)
