"""방사선 안전관리 — 피폭선량 누적·연한도·장비 정기검사 만기."""
from datetime import date

from erp.regulatory.models import RadiationEquipment
from erp.regulatory.radiation_service import RadiationService

svc = RadiationService()


async def test_dose_ytd_and_limit(session):
    r1 = await svc.record_dose(session, employee_id=5, period="2026-Q1", dose_msv=20,
                               measured_on=date(2026, 3, 31))
    assert r1["ytd_msv"] == 20.0 and r1["over_limit"] is False
    r2 = await svc.record_dose(session, employee_id=5, period="2026-Q2", dose_msv=35,
                               measured_on=date(2026, 6, 30))
    assert r2["ytd_msv"] == 55.0 and r2["over_limit"] is True   # 연 50mSv 초과
    # 동일 기간 재기록 — 갱신(누적 중복 없음)
    r3 = await svc.record_dose(session, employee_id=5, period="2026-Q2", dose_msv=10,
                               measured_on=date(2026, 6, 30))
    assert r3["ytd_msv"] == 30.0


async def test_inspections_due(session):
    session.add_all([
        RadiationEquipment(equipment_no="XR-1", name="X-ray", next_inspection_on=date(2026, 6, 1)),
        RadiationEquipment(equipment_no="CT-1", name="CT", next_inspection_on=date(2027, 1, 1)),
    ])
    await session.flush()
    out = await svc.inspections_due(session, as_of=date(2026, 6, 20), within_days=30)
    nos = [i["equipment_no"] for i in out["items"]]
    assert "XR-1" in nos and "CT-1" not in nos       # XR만 만기 임박/경과
    assert out["items"][0]["overdue"] is True


async def test_doses_by_employee_grouping(session):
    await svc.record_dose(session, employee_id=7, period="2026-Q1", dose_msv=10,
                          measured_on=date(2026, 3, 31))
    await svc.record_dose(session, employee_id=7, period="2026-Q2", dose_msv=45,
                          measured_on=date(2026, 6, 30))
    await svc.record_dose(session, employee_id=8, period="2026-Q1", dose_msv=5,
                          measured_on=date(2026, 3, 31))
    await svc.record_dose(session, employee_id=8, period="2025-Q4", dose_msv=99,
                          measured_on=date(2025, 12, 31))   # 타 연도 — 제외
    out = await svc.doses_by_employee(session, year="2026")
    assert out["annual_limit"] == 50.0
    # YTD 내림차순: emp7(55·초과) 먼저, emp8(5)
    assert [i["employee_id"] for i in out["items"]] == [7, 8]
    assert out["items"][0]["ytd_msv"] == 55.0 and out["items"][0]["over_limit"] is True
    assert len(out["items"][0]["entries"]) == 2                # 2026 2건만(2025 제외)
    assert out["items"][1]["ytd_msv"] == 5.0 and out["items"][1]["over_limit"] is False


async def test_list_equipment_overdue_flag(session):
    session.add_all([
        RadiationEquipment(equipment_no="XR-9", name="X-ray", next_inspection_on=date(2026, 1, 1)),
        RadiationEquipment(equipment_no="CT-9", name="CT", next_inspection_on=date(2027, 1, 1)),
    ])
    await session.flush()
    out = await svc.list_equipment(session, as_of=date(2026, 6, 20))
    # 만기 임박순(next_inspection_on) — XR-9(경과) 먼저
    assert out["items"][0]["equipment_no"] == "XR-9" and out["items"][0]["overdue"] is True
    assert out["items"][1]["equipment_no"] == "CT-9" and out["items"][1]["overdue"] is False
