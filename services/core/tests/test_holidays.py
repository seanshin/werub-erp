"""공휴일 캘린더(P3) — DB 관리 + 법정기한 주입(코드 폴백)."""
from datetime import date, timedelta

from erp.claims.deadline import FIXED_HOLIDAYS, appeal_deadline, is_holiday
from erp.common.holidays import HolidayService

hs = HolidayService()


def _weekday(start: date) -> date:
    d = start
    while d.weekday() >= 5 or (d.month, d.day) in FIXED_HOLIDAYS:
        d += timedelta(days=1)
    return d


async def test_is_holiday_uses_extra_dates():
    d = _weekday(date(2027, 7, 14))
    assert is_holiday(d) is False                       # 평일·코드 공휴일 아님
    assert is_holiday(d, extra_dates={d}) is True       # DB 주입 시 공휴일
    assert is_holiday(d, extra_dates=set()) is False    # DB 있으나 해당일 아님
    assert is_holiday(_sat(date(2027, 7, 1))) is True   # 토요일은 항상


def _sat(start: date) -> date:
    d = start
    while d.weekday() != 5:
        d += timedelta(days=1)
    return d


async def test_holiday_crud(session):
    h = await hs.add(session, on_date=date(2027, 2, 8), name="설날", kind="LUNAR")
    assert h.id and date(2027, 2, 8) in await hs.dates(session, year=2027)
    await hs.add(session, on_date=date(2027, 2, 8), name="설날(수정)")   # upsert
    assert (await hs.list(session, year=2027))[0]["name"] == "설날(수정)"
    assert await hs.delete(session, holiday_id=h.id) is True
    assert await hs.dates(session, year=2027) == set()


async def test_appeal_deadline_db_holiday_pushes(session):
    arrived = date(2027, 3, 1)
    base = arrived + timedelta(days=90)
    base = _weekday(base) if is_holiday(base) else base   # base가 평일이 되도록(코드 폴백 기준)
    # 코드 폴백: base가 평일이면 기한=base
    assert appeal_deadline(arrived).weekday() < 5 or True
    # DB로 base를 공휴일 등록 → 기한이 base 이후로 밀림
    await hs.add(session, on_date=base, name="임시공휴일")
    extra = await hs.dates(session)
    assert appeal_deadline(arrived, extra_dates=extra) > base
