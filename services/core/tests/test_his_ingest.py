"""HIS 환자·수납 스테이징 소비 (FR-15/16).

검증: NEW 스테이징 → 차트번호 upsert(전화 암호화·평문 파기) / 접수·수납 생성·마감·미매핑 격리.
⚠ 능동수집 REST 어댑터(구 integrations/his.py)는 §B 확정으로 2026-06-30 삭제 — 스테이징은
job_his_sync가 드레인하고 live 적재는 his_billing가 담당. 본 모듈은 소비 불변식만 검증한다.
"""
from datetime import date, datetime, timezone

from sqlalchemy import select

from erp.common import crypto
from erp.patient.ingest import consume_patient_staging, consume_payment_staging
from erp.patient.models import (
    DailyClosing,
    HISPatientStaging,
    HISPaymentStaging,
    Patient,
    Payment,
    Reception,
)

UTC = timezone.utc


# ── 환자 소비: upsert + 전화 암호화 ──────────────────────────
async def test_consume_patient_creates_and_encrypts(session):
    session.add(HISPatientStaging(
        source_ref="C-001:t1", chart_no="C-001", name="김환자", birth_date=date(1980, 3, 15),
        phone="010-1111-2222", source_seen_at=datetime(2026, 5, 6, 9, 0, tzinfo=UTC), status="NEW"))
    await session.flush()

    assert await consume_patient_staging(session) == {"created": 1, "updated": 0, "failed": 0}
    p = (await session.execute(select(Patient).where(Patient.chart_no == "C-001"))).scalar_one()
    assert p.phone_enc is not None                                   # 평문 미저장
    assert p.phone_bidx == crypto.phone_blind_index("010-1111-2222")  # blind index 검색 가능
    # H-2: 코어 암호화 반영 후 staging 평문 전화 즉시 파기 (§6.4)
    st = (await session.execute(select(HISPatientStaging))).scalar_one()
    assert st.status == "APPLIED" and st.phone is None
    # 재소비 no-op (APPLIED)
    assert await consume_patient_staging(session) == {"created": 0, "updated": 0, "failed": 0}


async def test_consume_patient_updates_existing(session):
    session.add(Patient(chart_no="C-001", name="구이름", birth_date=date(1980, 3, 15)))
    await session.flush()
    session.add(HISPatientStaging(
        source_ref="C-001:t2", chart_no="C-001", name="새이름", birth_date=date(1980, 3, 15),
        phone=None, source_seen_at=datetime(2026, 5, 7, 9, 0, tzinfo=UTC), status="NEW"))
    await session.flush()

    assert await consume_patient_staging(session) == {"created": 0, "updated": 1, "failed": 0}
    p = (await session.execute(select(Patient).where(Patient.chart_no == "C-001"))).scalar_one()
    assert p.name == "새이름"


# ── 수납 소비: 접수·수납 생성 / 격리 ────────────────────────
async def _seed_patient(session, chart_no="C-001"):
    p = Patient(chart_no=chart_no, name="김환자", birth_date=date(1980, 3, 15))
    session.add(p)
    await session.flush()
    return p


async def test_consume_payment_creates_reception_and_payment(session):
    await _seed_patient(session)
    session.add(HISPaymentStaging(
        source_ref="PAY-1", chart_no="C-001", amount=15000, method="CARD",
        paid_on=date(2026, 5, 6), dept_code="IM", doctor_id=7, status="NEW"))
    await session.flush()

    assert await consume_payment_staging(session) == {"applied": 1, "failed": 0}
    rec = (await session.execute(select(Reception))).scalar_one()
    assert rec.source == "HIS" and rec.dept_code == "IM"
    pay = (await session.execute(select(Payment))).scalar_one()
    assert float(pay.amount) == 15000 and pay.paid_on == date(2026, 5, 6)


async def test_consume_payment_unmapped_chart_failed(session):
    session.add(HISPaymentStaging(
        source_ref="PAY-X", chart_no="GHOST", amount=1000, method="CARD",
        paid_on=date(2026, 5, 6), status="NEW"))
    await session.flush()

    assert await consume_payment_staging(session) == {"applied": 0, "failed": 1}
    row = (await session.execute(select(HISPaymentStaging))).scalar_one()
    assert row.status == "FAILED" and row.error_detail["reason"] == "unmapped_chart_no"


async def test_consume_payment_day_closed_failed(session):
    """마감된 일자 수납은 FR-54 — FAILED 격리(마감 원장 무결성)."""
    await _seed_patient(session)
    session.add(DailyClosing(closing_on=date(2026, 5, 6), seq=1, total_amount=0,
                             payment_count=0, status="CLOSED", closed_by=1))
    session.add(HISPaymentStaging(
        source_ref="PAY-2", chart_no="C-001", amount=5000, method="CASH",
        paid_on=date(2026, 5, 6), status="NEW"))
    await session.flush()

    assert await consume_payment_staging(session) == {"applied": 0, "failed": 1}
    row = (await session.execute(select(HISPaymentStaging))).scalar_one()
    assert row.status == "FAILED" and row.error_detail["reason"] == "day_closed"
    assert (await session.execute(select(Payment))).scalar_one_or_none() is None  # 미생성

# ── 심층 재검토 보완: 재구동·행별 격리 ──────────────────────
async def test_consume_payment_unmapped_redriven_when_patient_arrives(session):
    """순서 의존 — 수납이 환자보다 먼저 도착해 FAILED(unmapped)여도, 환자 도착 후 재구동 시 APPLIED."""
    session.add(HISPaymentStaging(
        source_ref="PAY-R", chart_no="C-LATE", amount=8000, method="CASH",
        paid_on=date(2026, 5, 6), status="NEW"))
    await session.flush()
    assert await consume_payment_staging(session) == {"applied": 0, "failed": 1}   # 환자 없음
    row = (await session.execute(select(HISPaymentStaging))).scalar_one()
    assert row.status == "FAILED" and row.error_detail["reason"] == "unmapped_chart_no"
    # 환자 늦게 도착 → 재구동 → 수납 정상 반영(영구 소실 방지)
    await _seed_patient(session, "C-LATE")
    assert await consume_payment_staging(session) == {"applied": 1, "failed": 0}
    assert (await session.execute(select(select(Payment).subquery()))).first() is not None
    assert (await session.execute(select(HISPaymentStaging))).scalar_one().status == "APPLIED"


async def test_consume_patient_poison_row_isolated(session):
    """행별 savepoint 격리 — 독성 1행(빈 차트)이 배치 전체를 롤백하지 않고 FAILED 격리."""
    session.add_all([
        HISPatientStaging(source_ref="OK:t", chart_no="C-OK", name="정상", birth_date=date(1990, 1, 1),
                          source_seen_at=datetime(2026, 5, 6, 9, 0, tzinfo=UTC), status="NEW"),
        HISPatientStaging(source_ref="BAD:t", chart_no="  ", name="독성", birth_date=date(1990, 1, 1),
                          source_seen_at=datetime(2026, 5, 6, 9, 0, tzinfo=UTC), status="NEW")])
    await session.flush()
    out = await consume_patient_staging(session)
    assert out["created"] == 1 and out["failed"] == 1
    # 정상 행은 반영 유지(독성 행 때문에 롤백되지 않음)
    assert (await session.scalar(select(Patient).where(Patient.chart_no == "C-OK"))) is not None
    bad = await session.scalar(select(HISPatientStaging).where(HISPatientStaging.chart_no == "  "))
    assert bad.status == "FAILED"


async def test_consume_patient_same_chart_twice_in_batch(session):
    """N+1 배치화 — 한 배치에 동일 chart_no 2행(생성 후 갱신)이 사전맵 갱신으로 정상 처리."""
    session.add_all([
        HISPatientStaging(source_ref="DUP:t1", chart_no="C-DUP", name="최초",
                          birth_date=date(1990, 1, 1), phone="010-0000-0001",
                          source_seen_at=datetime(2026, 5, 6, 9, 0, tzinfo=UTC), status="NEW"),
        HISPatientStaging(source_ref="DUP:t2", chart_no="C-DUP", name="갱신",
                          birth_date=date(1990, 1, 1), phone="010-0000-0002",
                          source_seen_at=datetime(2026, 5, 6, 10, 0, tzinfo=UTC), status="NEW"),
    ])
    await session.flush()
    out = await consume_patient_staging(session)
    assert out["created"] == 1 and out["updated"] == 1 and out["failed"] == 0
    # 단일 환자 행(중복 생성 없음)·최종 이름=갱신
    ps = (await session.scalars(select(Patient).where(Patient.chart_no == "C-DUP"))).all()
    assert len(ps) == 1 and ps[0].name == "갱신"
