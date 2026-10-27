"""데이터 보존연한 파기 대상 식별 (design §6.5) — 읽기 전용 카운트(삭제 없음)."""
from datetime import date

from erp.app.retention_service import RetentionService
from erp.hr.models import Employee
from erp.patient.models import BillingLine

svc = RetentionService()


async def test_identify_candidates_counts_past_retention(session):
    # 진료비 라인: 11년 전(파기대상) + 1년 전(보존중)
    session.add_all([
        BillingLine(line_id="OLD", chart_no="C1", code="A", treated_on=date(2014, 1, 1)),
        BillingLine(line_id="NEW", chart_no="C1", code="A", treated_on=date(2025, 1, 1)),
    ])
    # 퇴직 직원: 4년 전(파기대상) + 재직중
    session.add_all([
        Employee(employee_no="E-OLD", name="퇴직", hired_on=date(2010, 1, 1),
                 resigned_on=date(2021, 1, 1)),
        Employee(employee_no="E-CUR", name="재직", hired_on=date(2024, 1, 1)),
    ])
    await session.flush()
    out = await svc.identify_candidates(session, as_of=date(2026, 6, 30))
    by_cat = {i["category"]: i for i in out["items"]}
    assert by_cat["진료비 라인(진료기록)"]["candidates"] == 1     # 2014만(10년 경과)
    assert by_cat["진료비 라인(진료기록)"]["retention_years"] == 10
    assert by_cat["퇴직 직원 인사기록"]["candidates"] == 1        # 2021 퇴직(3년 경과)
    assert out["total_candidates"] >= 2
