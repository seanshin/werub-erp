"""컴플라이언스 만기·한도 통합(app 크로스도메인) — 긴급도 밴드·카테고리·한도 경보."""
from datetime import date

from erp.app.compliance_service import ComplianceDeadlineService
from erp.hr.models import (
    ComplianceRecord, Employee, InsuranceReportEvent, License, MedicalStaffContract,
)
from erp.regulatory.models import PatientSafetyIncident, RadiationDose, RadiationEquipment
from erp.scm.models import DrugRecall, Item, Lot, MedicalWasteLog, OutsourcingContract

svc = ComplianceDeadlineService()
AS_OF = date(2026, 7, 1)


async def _seed(session):
    e1 = Employee(employee_no="E1", name="김의사", hired_on=date(2020, 1, 1))
    e2 = Employee(employee_no="E2", name="이간호", hired_on=date(2021, 1, 1))
    session.add_all([e1, e2])
    await session.flush()
    session.add_all([
        # 면허 — 경과(-10)·D-7(+5)·무기한(제외)·범위밖(+200)·장기경과(-400)
        License(employee_id=e1.id, license_type="의사", license_no="D-1",
                expires_on=date(2026, 6, 21)),
        License(employee_id=e2.id, license_type="조무사", license_no="A-OLD",
                expires_on=date(2025, 5, 28)),   # ~400일 경과 → long_overdue
        License(employee_id=e2.id, license_type="간호사", license_no="N-1",
                expires_on=date(2026, 7, 6)),
        License(employee_id=e1.id, license_type="약사", license_no="P-1", expires_on=None),
        License(employee_id=e1.id, license_type="방사선사", license_no="R-1",
                expires_on=date(2027, 1, 20)),
        # 법정교육(D-30)·건강검진(경과)
        ComplianceRecord(employee_id=e1.id, kind="TRAINING", category="감염관리",
                         done_on=date(2025, 7, 1), valid_until=date(2026, 7, 25)),
        ComplianceRecord(employee_id=e2.id, kind="CHECKUP", category="특수건강진단",
                         done_on=date(2025, 6, 1), valid_until=date(2026, 6, 15)),
        # 4대보험 신고 — PENDING(D-3)·REPORTED(제외)
        InsuranceReportEvent(employee_id=e2.id, event="ACQUIRE", basis_on=date(2026, 6, 25),
                             due_on=date(2026, 7, 4), status="PENDING"),
        InsuranceReportEvent(employee_id=e1.id, event="LOSE", basis_on=date(2026, 6, 1),
                             due_on=date(2026, 6, 10), status="REPORTED"),
        # 방사선 장비검사(D-2)
        RadiationEquipment(equipment_no="XR-1", name="흉부 X선",
                           next_inspection_on=date(2026, 7, 3)),
    ])
    # 재고 유효기간 — 임박(D-20, 잔량>0)·소진(제외)·범위밖(제외)
    it = Item(code="MED-1", name="수액", category="DRUG")
    session.add(it)
    await session.flush()
    session.add_all([
        Lot(item_id=it.id, lot_no="L1", expiry_on=date(2026, 7, 21), received_on=date(2026, 1, 1),
            received_qty=100, remaining_qty=30),
        Lot(item_id=it.id, lot_no="L2", expiry_on=date(2026, 7, 10), received_on=date(2026, 1, 1),
            received_qty=100, remaining_qty=0),
    ])
    # 방사선 피폭 — e1 초과(55)·e2 근접(42=84%)
    session.add_all([
        RadiationDose(employee_id=e1.id, period="2026-Q1", dose_msv=30, measured_on=date(2026, 3, 1)),
        RadiationDose(employee_id=e1.id, period="2026-Q2", dose_msv=25, measured_on=date(2026, 6, 1)),
        RadiationDose(employee_id=e2.id, period="2026-Q1", dose_msv=42, measured_on=date(2026, 3, 1)),
    ])
    # 환자안전 미검토 — REPORTED(카운트)·CLOSED(제외)
    session.add_all([
        PatientSafetyIncident(occurred_on=date(2026, 6, 20), incident_type="ADVERSE",
                              severity="MILD", description="낙상", status="REPORTED"),
        PatientSafetyIncident(occurred_on=date(2026, 5, 1), incident_type="NEAR_MISS",
                              severity="NONE", description="근접오류", status="CLOSED"),
    ])
    await session.flush()
    return e1, e2


async def test_deadlines_bands_and_categories(session):
    await _seed(session)
    out = await svc.deadlines(session, as_of=AS_OF, horizon_days=90)
    s = out["summary"]
    # 면허3(경과·D7·장기경과) + 교육1 + 검진1(경과) + 4대보험1 + 장비1 + 재고1
    # = 8건 — 법정 파라미터(NP 상·하한 7월분)는 2026 요율에 '7월분부터' 상·하한이 있어 경보 없음(2026-10-10)
    assert s["total"] == 8
    assert s["long_overdue"] == 1                   # 조무사 ~400일 경과
    assert s["actionable"] == 7                      # 장기경과 제외
    assert s["overdue"] == 2                          # 의사 면허·특수건강진단(180일 내)
    # 카테고리 요약은 실행 대상만 — 장기경과 조무사 제외돼 면허=2
    assert s["by_category"]["면허"] == 2
    assert "법정 파라미터" not in s["by_category"]   # 7월분 상·하한 반영됨
    assert s["by_category"]["법정교육"] == 1 and s["by_category"]["건강검진"] == 1
    assert s["by_category"]["4대보험 신고"] == 1
    # 장기경과는 목록 하단(정렬 — 실행 항목이 위)
    assert out["items"][-1]["band"] == "long_overdue"
    # 무기한 면허(약사)·범위밖(방사선사)은 미포함
    titles = " ".join(i["title"] for i in out["items"])
    assert "약사" not in titles and "방사선사" not in titles


async def test_deadlines_includes_mandatory_training(session):
    """P1-2 — 원내 필수교육 미이수(NONE)·만료(OVERDUE)가 만기 대시보드에 편입."""
    from erp.hr.training_service import TrainingService
    ts = TrainingService()
    e = Employee(employee_no="T1", name="박직원", hired_on=date(2025, 1, 1),
                 job_group="STAFF", employment_type="REGULAR", source="ERP")
    session.add(e)
    await session.flush()
    c = await ts.create_course(session, actor=1, code="INF", title="감염관리교육",
                               category="INFECTION", cycle_months=12, mandatory=True)
    out = await svc.deadlines(session, as_of=AS_OF, horizon_days=90)
    # 미이수(NONE) → 원내교육 카테고리로 편입
    assert out["summary"]["by_category"].get("원내교육") == 1
    assert any(i["category"] == "원내교육" and i["status"] == "미이수" for i in out["items"])
    # 만료 케이스 — 작년 이수(2025-06+12M=2026-06 < 7/1 as_of)
    s = await ts.create_session(session, actor=1, course_id=c.id, held_on=date(2025, 6, 1))
    await ts.record_completion(session, actor=1, session_id=s.id, employee_id=e.id)
    out2 = await svc.deadlines(session, as_of=AS_OF, horizon_days=90)
    assert any(i["category"] == "원내교육" and i["status"] == "이수만료" for i in out2["items"])


async def test_deadlines_alerts_radiation_and_safety(session):
    await _seed(session)
    out = await svc.deadlines(session, as_of=AS_OF, horizon_days=90)
    a = out["alerts"]
    assert [x["employee"] for x in a["radiation_over_limit"]] == ["김의사"]   # 55 > 50
    assert a["radiation_over_limit"][0]["ytd_msv"] == 55.0
    assert [x["employee"] for x in a["radiation_near_limit"]] == ["이간호"]   # 42 ≥ 40(80%)
    assert a["safety_unreviewed"] == 1                                        # REPORTED만


async def test_deadlines_horizon_narrows(session):
    await _seed(session)
    out = await svc.deadlines(session, as_of=AS_OF, horizon_days=7)   # D-7 이내만
    # 경과 포함 + 7일 이내: 의사면허(-10)·조무사(-400 장기)·간호면허(+5)·검진(-16)·4대보험(+3)·
    # 장비(+2) = 6(NP 7월분은 2026 요율에 반영돼 경보 없음), 교육(+24)·재고(+20) 제외
    assert out["summary"]["total"] == 6
    assert out["summary"]["actionable"] == 5          # 장기경과 조무사 제외
    assert all(i["days_remaining"] <= 7 for i in out["items"])


async def test_deadlines_contract_sources(session):
    """계약 만기 소스(위수탁·구매·의료진·계약직) 통합 — 유효 상태·종료일 설정분만."""
    e1 = Employee(employee_no="C1", name="박계약", hired_on=date(2025, 1, 1),
                  employment_type="CONTRACT", contract_end=date(2026, 7, 20))
    e2 = Employee(employee_no="C2", name="정정규", hired_on=date(2020, 1, 1))  # 정규직·contract_end None
    e3 = Employee(employee_no="C3", name="한퇴사", hired_on=date(2024, 1, 1),
                  employment_type="CONTRACT", contract_end=date(2026, 7, 10),
                  resigned_on=date(2026, 6, 30))  # 퇴사 → 제외
    session.add_all([e1, e2, e3])
    await session.flush()
    session.add_all([
        # 위수탁 — ACTIVE(D-15)·EXPIRED(제외)·자동갱신(D-25)
        OutsourcingContract(vendor_name="위탁급식(주)", category="MEAL", title="급식 위탁",
                            start_on=date(2025, 7, 1), end_on=date(2026, 7, 16), status="ACTIVE"),
        OutsourcingContract(vendor_name="구식업체", category="LAUNDRY", title="세탁(만료)",
                            start_on=date(2024, 1, 1), end_on=date(2026, 6, 1), status="EXPIRED"),
        OutsourcingContract(vendor_name="보안업체", category="SECURITY", title="경비 위탁",
                            start_on=date(2025, 1, 1), end_on=date(2026, 7, 26), status="EXPIRING",
                            auto_renew=True),
        # 의료진 계약 — ACTIVE·valid_to(D-5)·valid_to None(제외)
        MedicalStaffContract(employee_id=e2.id, valid_from=date(2025, 1, 1),
                             valid_to=date(2026, 7, 6), base_pay=10_000_000, status="ACTIVE"),
        MedicalStaffContract(employee_id=e1.id, valid_from=date(2025, 1, 1),
                             valid_to=None, base_pay=8_000_000, status="ACTIVE"),
    ])
    await session.flush()
    out = await svc.deadlines(session, as_of=AS_OF, horizon_days=90)
    cat = out["summary"]["by_category"]
    assert cat.get("위수탁·구매 계약") == 2          # ACTIVE·EXPIRING(자동갱신 포함)·EXPIRED 제외
    assert cat.get("의료진 계약") == 1                # valid_to 설정분만
    assert cat.get("근로계약(계약직)") == 1           # 재직 계약직만(퇴사·정규 제외)
    # 자동갱신 계약은 status 표기
    oc = next(i for i in out["items"] if i["title"] == "경비 위탁")
    assert oc["status"] == "자동갱신"
    # 퇴사 계약직(한퇴사)·정규직(정정규)·만료 위탁은 미포함
    titles = " ".join(i["title"] + (i["subject"] or "") for i in out["items"])
    assert "한퇴사" not in titles and "세탁(만료)" not in titles


async def test_deadlines_waste_and_recall(session):
    """의료폐기물 법정 보관기한(기한형) + 의약품 회수 미조치(경보형)."""
    it = Item(code="D-1", name="주사제", category="DRUG")
    session.add(it)
    await session.flush()
    session.add_all([
        # 폐기물 — 일반(15일 한도): 6/20 보관 → due 7/5(D+4, 포함)·인계완료(제외)·손상성 6/5→due 7/5
        MedicalWasteLog(waste_type="일반", weight_kg=3.2, stored_on=date(2026, 6, 20), status="STORED"),
        MedicalWasteLog(waste_type="격리", weight_kg=1.0, stored_on=date(2026, 5, 1),
                        status="HANDED_OVER"),   # 인계완료 → 제외
        MedicalWasteLog(waste_type="손상성", weight_kg=0.5, stored_on=date(2026, 6, 5),
                        status="STORED"),          # 30일 한도 → due 7/5(포함)
        # 의약품 회수 — OPEN(경보)·CLOSED(제외)
        DrugRecall(item_id=it.id, reason="이물질", severity="CLASS2", recall_on=date(2026, 6, 25),
                   status="OPEN"),
        DrugRecall(item_id=it.id, reason="표시오류", severity="VOLUNTARY", recall_on=date(2026, 5, 1),
                   status="CLOSED"),
    ])
    await session.flush()
    out = await svc.deadlines(session, as_of=AS_OF, horizon_days=90)
    assert out["summary"]["by_category"]["의료폐기물 보관"] == 2   # 일반+손상성(인계완료 제외)
    assert out["alerts"]["drug_recall_open"] == 1                   # OPEN만
    # 손상성 30일 한도 정확 — 6/5 보관 → 7/5 만기
    dmg = next(i for i in out["items"] if "손상성" in i["title"])
    assert dmg["due_on"] == "2026-07-05"



async def test_np_july_bounds_alert_only_when_missing(session, monkeypatch):
    """그 해 요율에 '7월분부터' 상·하한이 없을 때만 연례 점검 경보 — 출구는 관리화면 칸(코드 RATES 아님)."""
    from erp.hr import statutory
    no_h2 = {k: v for k, v in statutory.RATES[2026].items() if not k.endswith("_h2")}
    monkeypatch.setitem(statutory.RATES, 2026, no_h2)
    out = await svc.deadlines(session, as_of=AS_OF, horizon_days=90)
    t = [i["title"] for i in out["items"] if i["category"] == "법정 파라미터"]
    assert t and "7월분" in t[0] and "/admin/payroll-settings" in t[0]


async def test_next_year_holidays_alert_until_registered(session):
    """연말 90일 이내 + 이듬해 공휴일 0건이면 경보(출구 /admin/org) — 1건이라도 등록되면 사라진다(2026-10-10)."""
    from erp.common.models import Holiday
    as_of = date(2026, 10, 10)
    t = [i["title"] for i in (await svc.deadlines(session, as_of=as_of, horizon_days=90))["items"]
         if "공휴일" in i["title"]]
    assert t and "2027년" in t[0] and "/admin/org" in t[0]
    session.add(Holiday(on_date=date(2027, 2, 8), name="설날", kind="LUNAR"))
    await session.flush()
    t = [i["title"] for i in (await svc.deadlines(session, as_of=as_of, horizon_days=90))["items"]
         if "공휴일" in i["title"]]
    assert not t
    early = [i["title"] for i in (await svc.deadlines(session, as_of=date(2026, 7, 1), horizon_days=90))["items"]
             if "2027년 공휴일" in i["title"]]
    assert not early                                  # 12/31이 지평선 밖이면 경보 없음
