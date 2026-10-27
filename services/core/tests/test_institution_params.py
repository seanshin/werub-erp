"""기관 규제 프로필 + 분기 재원환자수 대장 — 간호등급 산정 입력 관리."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.regulatory.institution_service import (
    InstitutionProfileService, NursingCensusService,
)
from erp.regulatory.nursing_grade import quarter_window
from erp.regulatory.nursing_monitor import NursingMonitor

prof = InstitutionProfileService()
census = NursingCensusService()


async def test_profile_effective_default(session):
    """미설정 시 안전 기본값 HOSPITAL·source=default."""
    eff = await prof.effective(session)
    assert eff["hospital_type"] == "HOSPITAL" and eff["source"] == "default"


async def test_profile_upsert_and_validate(session):
    row = await prof.upsert(session, actor=1, hospital_type="GENERAL", licensed_beds=300)
    assert row.hospital_type == "GENERAL" and row.licensed_beds == 300
    eff = await prof.effective(session)
    assert eff["source"] == "db" and eff["hospital_type"] == "GENERAL"
    with pytest.raises(ValidationError):
        await prof.upsert(session, actor=1, hospital_type="BOGUS")
    with pytest.raises(ValidationError):
        await prof.upsert(session, actor=1, licensed_beds=-5)


async def test_monitor_uses_profile_type(session):
    """monitor가 종별 미지정 시 프로필 종별 사용."""
    await prof.upsert(session, actor=1, hospital_type="TERTIARY")
    out = await NursingMonitor().monitor(session)     # hospital_type 미지정
    assert out["hospital_type"] == "TERTIARY"


async def test_monitor_uses_saved_census_priority(session):
    """저장 census가 추정보다 우선(수동 파라미터 없을 때). source=SAVED_CENSUS."""
    start, end = quarter_window(date.today())
    await census.upsert(session, actor=1, period_start=start, period_end=end,
                        patient_days=9000, note="분기 실측")
    out = await NursingMonitor().monitor(session)     # patient_days 미지정
    assert out["patient_days_source"] == "SAVED_CENSUS"
    assert out["patient_days"] == 9000.0
    # 수동 파라미터는 저장 census보다 우선
    out2 = await NursingMonitor().monitor(session, patient_days=1234)
    assert out2["patient_days_source"] == "MANUAL" and out2["patient_days"] == 1234


async def test_census_upsert_list_delete(session):
    start, end = quarter_window(date.today())
    r = await census.upsert(session, actor=1, period_start=start, period_end=end,
                            patient_days=100)
    # 같은 기간 재입력 = 갱신(중복행 없음)
    r2 = await census.upsert(session, actor=1, period_start=start, period_end=end,
                             patient_days=200)
    assert r.id == r2.id and float(r2.patient_days) == 200
    rows = await census.list(session)
    assert len(rows) == 1
    await census.delete(session, census_id=r.id)
    assert not await census.list(session)
    with pytest.raises(ValidationError):
        await census.delete(session, census_id=999999)
