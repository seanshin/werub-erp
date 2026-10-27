"""수당 단가 마스터 (P2-②) — DB 우선·기본값 폴백·build_payroll 주입."""
import pytest

from erp.errors import ValidationError
from erp.hr import payroll_rules
from erp.hr.pay_rates import list_rates, load_rates, upsert_rate


async def test_db_overrides_code_default(session):
    # DB 미설정 → 코드 기본값 (야간시급 12,000)
    r = payroll_rules.build_payroll(job_category="NURSE", base_pay=2_000_000,
                                    night_hours=10, rates=await load_rates(session))
    night = next(line for line in r["lines"] if line["item_code"] == "NIGHT")
    assert night["amount"] == 120_000

    # 단협 개정 — 야간시급 15,000으로 DB 변경
    await upsert_rate(session, actor=1, rate_key="NIGHT_HOURLY", amount=15_000)
    r2 = payroll_rules.build_payroll(job_category="NURSE", base_pay=2_000_000,
                                     night_hours=10, rates=await load_rates(session))
    night2 = next(line for line in r2["lines"] if line["item_code"] == "NIGHT")
    assert night2["amount"] == 150_000


async def test_list_marks_overridden_and_rejects_negative(session):
    await upsert_rate(session, actor=1, rate_key="DUTY_PAY", amount=120_000)
    items = {i["rate_key"]: i for i in await list_rates(session)}
    assert items["DUTY_PAY"]["overridden"] is True and items["DUTY_PAY"]["amount"] == 120_000
    assert items["NIGHT_HOURLY"]["overridden"] is False     # 미설정 → 기본값 표시
    assert items["NIGHT_HOURLY"]["amount"] == items["NIGHT_HOURLY"]["default"]
    with pytest.raises(ValidationError):
        await upsert_rate(session, actor=1, rate_key="DUTY_PAY", amount=-1)


async def test_custom_qualification_key(session):
    # 운영 추가 키 — 신규 직종 자격수당
    await upsert_rate(session, actor=1, rate_key="QUAL_DIETITIAN", amount=100_000,
                      description="영양사 자격수당")
    rates = await load_rates(session)
    r = payroll_rules.build_payroll(job_category="DIETITIAN", base_pay=2_500_000,
                                    rates=rates)
    qual = [line for line in r["lines"] if line["item_code"] == "QUAL"]
    assert qual and qual[0]["amount"] == 100_000


async def test_delete_rate_removes_misentered_row(session):
    """오등록 발효행 삭제 — 과거 소급 왜곡 복구(적대검토 MED)."""
    from datetime import date

    from erp.hr.pay_rates import delete_rate, rate_history
    # 미래 인상 의도였으나 과거 발효일로 오타 입력
    await upsert_rate(session, actor=1, rate_key="NIGHT_HOURLY", amount=99_000,
                      valid_from=date(2025, 8, 1))
    assert len(await rate_history(session, rate_key="NIGHT_HOURLY")) == 1
    # 삭제로 정정
    out = await delete_rate(session, actor=1, rate_key="NIGHT_HOURLY",
                            valid_from=date(2025, 8, 1))
    assert out["deleted"] is True
    assert await rate_history(session, rate_key="NIGHT_HOURLY") == []
    # 없는 행 삭제는 검증 오류
    with pytest.raises(ValidationError):
        await delete_rate(session, actor=1, rate_key="NIGHT_HOURLY",
                          valid_from=date(2025, 8, 1))


async def test_list_rates_surfaces_future_reserved(session):
    """미래 발효 예약행 노출(적대검토 LOW — '미등록' 오인·중복등록 방지)."""
    from datetime import date

    from erp.hr.pay_rates import list_rates
    # 오늘 유효 + 미래 예약
    await upsert_rate(session, actor=1, rate_key="NIGHT_HOURLY", amount=12_000,
                      valid_from=date(2026, 1, 1))
    await upsert_rate(session, actor=1, rate_key="NIGHT_HOURLY", amount=15_000,
                      valid_from=date(2027, 1, 1), description="2027 단협")
    items = {i["rate_key"]: i for i in await list_rates(session, as_of=date(2026, 8, 1))}
    n = items["NIGHT_HOURLY"]
    assert n["amount"] == 12_000 and n["next"] == {"valid_from": "2027-01-01", "amount": 15_000}


async def test_list_rates_future_only_key_visible(session):
    """현행 없이 미래 예약만 있는 커스텀 키도 목록 노출(next만)."""
    from datetime import date

    from erp.hr.pay_rates import list_rates
    await upsert_rate(session, actor=1, rate_key="QUAL_NEW", amount=50_000,
                      valid_from=date(2027, 6, 1))
    items = {i["rate_key"]: i for i in await list_rates(session, as_of=date(2026, 8, 1))}
    assert items["QUAL_NEW"]["amount"] is None
    assert items["QUAL_NEW"]["next"]["amount"] == 50_000
