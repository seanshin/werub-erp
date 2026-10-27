"""운영 정합성 점검 — 불변식 검사가 실제 결함을 잡고, 정상일 때 조용한지.

이 기능의 목적은 "조용히 틀린 채로 있다가 우연히 드러나던" 정합 문제를 상시 노출하는 것이다.
따라서 두 방향을 다 고정한다: **결함을 심으면 FAIL**, **정상이면 OK**(영구 발화 금지).
"""
from datetime import date

from erp.app.consistency_service import run_all
from erp.finance.models import Account, CorporateAccount, Receivable
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.patient.models import BillingLine, Patient

vs = VoucherService()


def _by_key(out):
    return {i["key"]: i for i in out["items"]}


async def _accounts(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1130", name="미수금", kind="ASSET"),
        Account(code="1115", name="외화예금", kind="ASSET"),
        Account(code="4110", name="수익", kind="REVENUE"),
    ])
    await session.flush()


async def test_all_ok_on_clean_db(session):
    """빈/정상 DB에서는 전부 OK 또는 SKIP — 상시 적색이면 진짜 신호를 가린다."""
    await _accounts(session)
    out = await run_all(session)
    assert out["overall"] == "OK"
    assert not [i for i in out["items"] if i["status"] in ("FAIL", "WARN")]


async def test_detects_currency_mix_and_patient_master_gap(session):
    """통화 혼입·환자 마스터 불일치 — 실제로 prod에서 발생했던 두 결함."""
    await _accounts(session)
    session.add_all([
        CorporateAccount(alias="USD", bank_name="신한", account_no="1-USD",
                         currency="USD", gl_account_code="1115"),
        CorporateAccount(alias="JPY", bank_name="국민", account_no="2-JPY",
                         currency="JPY", gl_account_code="1115"),   # 같은 GL — 혼입
        BillingLine(line_id="L1", chart_no="P2026-0001", code="AB1",
                    treated_on=date(2026, 5, 1), cancelled=False),  # 마스터에 없는 환자
    ])
    await session.flush()

    r = _by_key(await run_all(session))
    assert r["forex.gl_currency_mix"]["status"] == "FAIL"
    assert r["ar.patient_master_match"]["status"] == "FAIL"
    assert r["ar.patient_master_match"]["detail"]["missing"] == 1

    # 마스터를 채우면 해당 검사만 OK로 돌아온다(자가 해소)
    session.add(Patient(chart_no="P2026-0001", name="환자", birth_date=date(1990, 1, 1)))
    await session.flush()
    r2 = _by_key(await run_all(session))
    assert r2["ar.patient_master_match"]["status"] == "OK"
    assert r2["forex.gl_currency_mix"]["status"] == "FAIL"      # 이쪽은 그대로


async def test_detects_mirror_without_gl(session):
    """미러만 있고 GL 전기가 없는 채권 — 재무상태표에 음수 순채권을 만들었던 결함."""
    await _accounts(session)
    session.add(Receivable(party="A", amount=1_000_000, collected_amount=0,
                           billed_on=date(2026, 5, 1), due_on=date(2026, 5, 31),
                           status="OUTSTANDING", source_module="demo"))
    await session.flush()
    r = _by_key(await run_all(session))
    assert r["ar.mirror_vs_gl"]["status"] == "WARN"
    assert [d for d in r["ar.mirror_vs_gl"]["detail"]
            if d["account"] == "1130"][0]["diff"] == 1_000_000

    # GL을 채우면 해소
    await vs.create(session, source_module="demo", source_ref="ar-1",
                    voucher_on=date(2026, 5, 1),
                    lines=[VoucherLineDTO("1130", "DEBIT", 1_000_000),
                           VoucherLineDTO("4110", "CREDIT", 1_000_000)])
    assert _by_key(await run_all(session))["ar.mirror_vs_gl"]["status"] == "OK"


async def test_accrual_prereq_skipped_when_gate_off(session, monkeypatch):
    """게이트가 꺼져 있으면 SKIP — 활동 기반 검사를 조건부 불변식으로 만든 지점."""
    from erp.config import get_settings
    s = get_settings()
    old = s.patient_accrual_enabled
    try:
        monkeypatch.setattr(s, "patient_accrual_enabled", False, raising=False)
        assert _by_key(await run_all(session))["ar.accrual_prerequisites"]["status"] == "SKIP"
        monkeypatch.setattr(s, "patient_accrual_enabled", True, raising=False)
        # 켜면 수납 이벤트 0건이므로 FAIL — "발생만 켜면 1131이 차변으로만 쌓인다"
        assert _by_key(await run_all(session))["ar.accrual_prerequisites"]["status"] == "FAIL"
    finally:
        s.patient_accrual_enabled = old


async def test_endpoint_registered_and_gated(session, client):
    """HTTP 계층 — 라우터 등록·RBAC(admin.audit read=DIRECTOR/ADMIN)."""
    from tests._http import auth_as
    await _accounts(session)
    admin = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.get("/api/v1/admin/consistency", headers=admin)
    assert r.status_code == 200 and r.json()["overall"] in ("OK", "WARN", "FAIL")

    staff = await auth_as(session, user_id=2, role="STAFF")
    assert (await client.get("/api/v1/admin/consistency", headers=staff)).status_code == 403


# ---- 발생주의 전기 전 전제 가드 ----

async def test_accrual_reports_warnings_without_blocking(session, monkeypatch):
    """정합 이상이 있어도 **배치는 계속 돌고**, 위반은 결과의 warnings로 나온다.

    ⚠ 처음엔 하드블록으로 만들었다가 정상 상태를 막아 두 번 좁히고 결국 경고로 바꿨다:
    마스터 누락 1건으로 야간 배치를 세우면 건별 격리 설계가 무력해지고, **개통 첫날은
    수납이 없는 것이 정상**이라 시점 상태로는 막을 수 없다.
    """
    from erp.config import get_settings
    from erp.patient.receivable_service import PatientReceivableService

    await _accounts(session)
    session.add(BillingLine(line_id="L9", chart_no="P2026-9999", code="AB1",
                            treated_on=date(2026, 5, 1), cancelled=False))  # 마스터에 없음
    await session.flush()

    cfg = get_settings()
    old = cfg.patient_accrual_enabled
    try:
        monkeypatch.setattr(cfg, "patient_accrual_enabled", True, raising=False)
        out = await PatientReceivableService().accrue_all(session, care_month="2026-05")
        assert out["enabled"] is True                     # 차단되지 않는다
        keys = {w["key"] for w in out["warnings"]}
        assert "ar.patient_master_match" in keys          # 마스터 누락
        assert "ar.accrual_prerequisites" in keys         # 수납 기록 없음
    finally:
        cfg.patient_accrual_enabled = old


async def test_accrual_not_blocked_when_gate_off(session):
    """게이트가 꺼져 있으면 전제를 요구하지 않는다 — 끄고 쓰는 것은 막지 않는다."""
    from erp.patient.receivable_service import PatientReceivableService
    out = await PatientReceivableService().accrue_all(session, care_month="2026-05")
    assert out["enabled"] is False


async def test_audit_handler_notifies_only_on_anomaly(session):
    """정합 이상이 있을 때만 관리자 알림 — 정상이면 무알림(영구 발화 금지)."""
    from erp.app.recurring_handlers import consistency_audit
    from erp.common.models import User

    await _accounts(session)
    session.add(User(id=90, email="a@t.example", role="ADMIN", password_hash="x",
                     token_version=0, is_active=True))
    await session.flush()

    out = await consistency_audit(session, "2026-07-29", {})
    assert out["overall"] == "OK" and out["notified"] == 0     # 정상 → 조용

    session.add_all([
        CorporateAccount(alias="U", bank_name="a", account_no="1", currency="USD",
                         gl_account_code="1115"),
        CorporateAccount(alias="J", bank_name="b", account_no="2", currency="JPY",
                         gl_account_code="1115"),
    ])
    await session.flush()
    out = await consistency_audit(session, "2026-07-29", {})
    assert out["overall"] == "FAIL" and out["notified"] >= 1
    assert "forex.gl_currency_mix" in out["failed"]


# ---- scm·claims 확장(v1.280) ----

async def test_detects_stock_over_received(session):
    """잔량이 입고량을 넘으면 FAIL — 반품·정정 과대 반제 신호.

    ⚠ 음수 잔량은 검사 대상이 아니다 — DB CHECK가 이미 막아 **영원히 걸리지 않기** 때문이다
    (실제로 이 테스트를 쓰다가 `ck_lot_nonneg`에 막혀서 알았다).
    """
    from erp.scm.models import Item, Lot

    await _accounts(session)
    it = Item(code="I1", name="품목", category="SUPPLY", is_narcotic=False, safety_stock_qty=0)
    session.add(it)
    await session.flush()
    assert _by_key(await run_all(session))["scm.stock_within_received"]["status"] == "OK"

    session.add(Lot(item_id=it.id, lot_no="L1", expiry_on=date(2027, 1, 1),
                    received_on=date(2026, 5, 1), received_qty=10, remaining_qty=15))
    await session.flush()
    r = _by_key(await run_all(session))["scm.stock_within_received"]
    assert r["status"] == "FAIL" and r["detail"]["lot_over_received"] == 1


async def test_claim_recon_ignores_months_without_submission(session):
    """제출건이 없는 월은 불일치가 아니다 — 이 구분이 없으면 최근 월이 상시 적색이 된다."""
    from erp.claims.models import ClaimBatch, ClaimStatement

    await _accounts(session)
    b = ClaimBatch(care_month="2026-08", status="DECIDED", receipt_no="R-2026-08",
                   notice_arrived_on=date(2026, 9, 1))
    session.add(b)
    await session.flush()
    session.add(ClaimStatement(batch_id=b.id, stmt_serial="S1", chart_no="P1",
                               claimed_amt=100_000))
    await session.flush()
    # 제출건 0 → 통보서 미도착으로 보고 경고하지 않는다
    assert _by_key(await run_all(session))["claims.submission_vs_statement"]["status"] == "OK"


async def test_mirror_maps_patient_receivable_to_1131(session):
    """환자채권(source_module='patient')은 GL **1131**과 대조한다 — 1130과 비교하면
    발생주의를 켜는 순간 **영구 WARN**이 된다(이 서비스가 금지한 안티패턴)."""
    from erp.finance.models import Receivable

    await _accounts(session)
    session.add(Account(code="1131", name="환자미수금", kind="ASSET"))
    session.add(Receivable(party="P1", amount=500_000, collected_amount=0,
                           billed_on=date(2026, 5, 1), due_on=date(2026, 5, 31),
                           status="OUTSTANDING", source_module="patient"))
    await session.flush()

    r = _by_key(await run_all(session))["ar.mirror_vs_gl"]
    accts = {d["account"] for d in r["detail"]}
    assert accts == {"1131"}                       # 1130이 아니라 1131로 간다
    assert [d for d in r["detail"] if d["account"] == "1131"][0]["mirror"] == 500_000

    # GL 1131을 채우면 해소
    await vs.create(session, source_module="patient-ar", source_ref="pa-1",
                    voucher_on=date(2026, 5, 31),
                    lines=[VoucherLineDTO("1131", "DEBIT", 500_000),
                           VoucherLineDTO("4110", "CREDIT", 500_000)])
    assert _by_key(await run_all(session))["ar.mirror_vs_gl"]["status"] == "OK"


async def test_orphan_payroll_voucher_detected(session):
    """급여기록이 지워졌는데 전표만 남으면 FAIL — 병합이 실제로 만든 적 있는 결함.

    ⚠ 이 검사는 `func.concat`+cast로 source_ref를 조립한다. 방언마다 동작이 달라 **조용히
    0건만 반환하는(거짓 OK)** 위험이 있어 양성 케이스로 고정한다.
    """
    await _accounts(session)
    session.add(Account(code="5310", name="급여", kind="EXPENSE"))
    await session.flush()
    await vs.create(session, source_module="hr.payroll", source_ref="payroll:99999",
                    voucher_on=date(2026, 7, 25),
                    lines=[VoucherLineDTO("5310", "DEBIT", 1000),
                           VoucherLineDTO("1110", "CREDIT", 1000)])
    r = _by_key(await run_all(session))["ledger.orphan_payroll_vouchers"]
    assert r["status"] == "FAIL" and r["detail"]["count"] == 1
