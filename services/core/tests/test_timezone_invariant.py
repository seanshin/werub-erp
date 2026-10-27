"""TZ 불변식 회귀 — naive KST를 timestamptz에 쓰는 경로의 전제를 고정한다.

배경(적대검토 A-M2·B-F5·D): asyncpg의 timestamptz 인코더는 naive datetime을 **쓰는 프로세스의
로컬 TZ**로 해석한다(`obj.astimezone(utc)`). 이 코드베이스는 근태 clock(`inbound_consumer`)과
정기업무 실행이력(`common.recurring.now_naive`)에서 의도적으로 naive KST를 쓰므로, 프로세스 TZ가
Asia/Seoul이 아니면 저장 순간이 ±9h 어긋나고 되읽기(`as_kst_wallclock`)가 왜곡된다.

prod는 `infra/.env`의 `TZ=Asia/Seoul` 한 줄에 이 정합이 매달려 있었다(2026-07-28 실측은 정상).
Dockerfile ENV·compose·CI로 이중화하고, 여기서 불변식 자체를 테스트로 고정한다.
"""
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from erp.common.kst import (
    KST,
    ProcessTimezoneError,
    as_kst_wallclock,
    assert_process_tz_kst,
    kst_now,
)
from erp.hr.models import GroupwareAttendanceStaging

_PG = os.environ.get("TEST_DATABASE_URL")


def test_process_tz_is_kst():
    """테스트 실행 환경 자체가 prod와 같은 KST여야 한다(CI가 TZ=Asia/Seoul 고정)."""
    assert_process_tz_kst()
    assert datetime.now().astimezone().utcoffset() == timedelta(hours=9)


def test_assert_raises_when_tz_not_kst():
    """TZ가 어긋나면 조용한 데이터 오염 대신 기동 실패 — 실제 프로세스 TZ를 바꿔 검증."""
    import time

    prev = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "UTC"
        time.tzset()
        with pytest.raises(ProcessTimezoneError):
            assert_process_tz_kst()
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()
    assert_process_tz_kst()   # 복원 확인 — 후속 테스트 오염 방지


def test_as_kst_wallclock_roundtrips_kst_now():
    """앱 생성 aware KST → 라벨 제거 → 동일 벽시계(백엔드 무관 항등)."""
    now = kst_now()
    assert as_kst_wallclock(now) == now.replace(tzinfo=None)


@pytest.mark.skipif(not _PG, reason="PG 레인 전용(TEST_DATABASE_URL)")
async def test_naive_kst_write_roundtrips_through_timestamptz(session):
    """naive KST write → timestamptz → as_kst_wallclock read 가 항등인지 실 DB로 검증.

    프로세스 TZ가 KST가 아니면 여기서 ±9h로 깨진다 — v1.257.x 계열 왜곡의 조기 경보.
    """
    wall = datetime(2026, 7, 28, 9, 0, 0)          # naive KST 벽시계(근태 clock 쓰기와 동형)
    session.add(GroupwareAttendanceStaging(
        source_ref="tz-invariant-1", external_user_id="tz-test", employee_no="TZ-TEST-001",
        work_on=wall.date(), clock_in=wall, status="NEW"))
    await session.flush()
    session.expunge_all()                            # 캐시 우회 — 진짜 되읽기
    row = (await session.scalars(select(GroupwareAttendanceStaging).where(
        GroupwareAttendanceStaging.employee_no == "TZ-TEST-001"))).one()
    assert row.clock_in.tzinfo is not None, "PG timestamptz는 aware로 되읽혀야 한다"
    assert row.clock_in.astimezone(KST).hour == 9
    assert as_kst_wallclock(row.clock_in) == wall


def test_kst_iso_matches_global_encoder():
    """수동 직렬화 지점이 전역 인코더와 같은 값을 내야 한다(D-H2)."""
    from datetime import timezone

    from erp.common.kst import api_datetime_encoder, kst_iso

    aware = datetime(2026, 7, 27, 22, 10, tzinfo=timezone.utc)   # KST 07-28 07:10
    assert kst_iso(aware) == api_datetime_encoder(aware)
    assert kst_iso(aware).startswith("2026-07-28T07:10")          # 날짜가 밀리지 않는다
    assert kst_iso(None) is None
    naive = datetime(2026, 7, 28, 9, 0)
    assert kst_iso(naive) == naive.isoformat()                    # naive는 그대로


def test_excel_export_uses_kst_for_datetime():
    """반출본이 UTC라 같은 데이터의 화면(KST)과 어긋나던 문제(D-M1)."""
    from datetime import date as _date, timezone

    from erp.common.excel import _coerce

    assert _coerce(datetime(2026, 7, 27, 22, 10, tzinfo=timezone.utc)).startswith("2026-07-28T07:10")
    assert _coerce(_date(2026, 7, 28)) == "2026-07-28"            # date는 무변경
