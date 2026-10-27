"""2026-10-09 전체 시나리오·통합 테스트(실 스택)가 찾은 결함 회귀 — xlsx 반출·복리후생 행 검증·메트릭 공개·
접근 로그 환자 식별자·법정 평균임금."""
from datetime import date

import pytest

from erp.common.models import Department
from erp.hr.models import Employee, PayrollLine, PayrollRecord
from tests._http import auth_as

V = "/api/v1"


# ── S-F D1: xlsx 반출 4종이 200·0바이트(서버는 'int' has no attribute 'encode') ─────────────
@pytest.mark.parametrize("path", [
    "/hr/compliance/status.xlsx",
    "/patients/medical-expenses.xlsx?year=2026",
    "/claims/pre-review/cases-export.xlsx",
    "/finance/statements/equity-changes.xlsx?year=2026",
])
async def test_xlsx_exports_return_real_workbook(client, session, path):
    hdr = await auth_as(session, role="ADMIN")
    r = await client.get(V + path, headers=hdr)
    assert r.status_code == 200
    assert r.content[:2] == b"PK" and len(r.content) > 100     # xlsx = zip


# ── S-F D3: DEPT 권한자가 타 부서 직원 id로 복리후생 잔액 조회 ──────────────────────────────
async def test_welfare_balance_dept_scope_checks_other_dept(client, session):
    session.add_all([Department(id=2, name="간호부"), Department(id=5, name="원무과")])
    other = Employee(employee_no="SCN-W1", name="타부서", hired_on=date(2025, 1, 1),
                     job_group="STAFF", employment_type="REGULAR", source="ERP", department_id=5)
    mine = Employee(employee_no="SCN-W2", name="같은부서", hired_on=date(2025, 1, 1),
                    job_group="STAFF", employment_type="REGULAR", source="ERP", department_id=2)
    session.add_all([other, mine])
    await session.flush()
    hdr = await auth_as(session, role="MANAGER", dept_id=2)
    r = await client.get(f"{V}/hr/welfare/balance?year=2026&employee_id={other.id}", headers=hdr)
    assert r.status_code == 403
    r = await client.get(f"{V}/hr/welfare/balance?year=2026&employee_id=999999", headers=hdr)
    assert r.status_code == 403
    r = await client.get(f"{V}/hr/welfare/balance?year=2026&employee_id={mine.id}", headers=hdr)
    assert r.status_code == 200


# ── S-F 위험(prod 실측 공개 200): 프록시 경유 /api/metrics는 토큰 필요 ─────────────────────
async def test_metrics_via_proxy_requires_token(client, session, monkeypatch):
    from erp.config import get_settings
    r = await client.get("/api/metrics", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 404
    r = await client.get("/api/metrics", headers={"CF-Connecting-IP": "203.0.113.9"})
    assert r.status_code == 404
    r = await client.get("/api/metrics", headers={"Forwarded": "for=203.0.113.9"})
    assert r.status_code == 404
    monkeypatch.setattr(get_settings(), "metrics_token", "scrape-secret")
    r = await client.get("/api/metrics", headers={"X-Forwarded-For": "203.0.113.9",
                                                  "Authorization": "Bearer wrong"})
    assert r.status_code == 404
    r = await client.get("/api/metrics", headers={"X-Forwarded-For": "203.0.113.9",
                                                  "Authorization": "Bearer scrape-secret"})
    assert r.status_code == 200 and "erp_up 1" in r.text
    r = await client.get("/api/metrics")                 # 내부망 직접 스크레이프는 그대로
    assert r.status_code == 200


# ── S-F D4: 접근 로그에 chart_no·이름·전화·생년월일 원문 ─────────────────────────────────────
def test_access_log_redacts_patient_identifiers():
    from erp.logging import redact_query_secrets as r
    line = r("GET /api/v1/patients?name=홍길동&phone=01099998888&birth_date=1990-01-01"
             "&chart_no=TEST-00051&code=AA157 HTTP/1.1")
    for raw in ("홍길동", "01099998888", "1990-01-01", "TEST-00051"):
        assert raw not in line
    assert "code=AA157" in line                         # 업무 코드는 관측성 유지(G13)
    assert "chartNo=***" in r("/api/v1/integration/billing/invoice?chartNo=123&page=2")
    assert "sheet_name=x" in r("/x?sheet_name=x")       # 접미 일치 오탐 없음
    assert "01099998888" not in r("/api/v1/patients/search?q=01099998888")
    assert "faq=1" in r("/x?faq=1")


# ── S-C: 일할된 퇴사월을 한 달로 세 평균임금·퇴직금 과소 ─────────────────────────────────────
async def _payroll(session, emp, ym, amount):
    rec = PayrollRecord(employee_id=emp.id, yyyymm=ym)
    session.add(rec)
    await session.flush()
    session.add(PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=amount))
    await session.flush()


async def test_statutory_avg_wage_prorated_final_month(session):
    from erp.hr.retirement_service import statutory_avg_wage
    emp = Employee(employee_no="SCN-R1", name="퇴사자", hired_on=date(2020, 1, 1),
                   resigned_on=date(2026, 9, 20), job_group="STAFF",
                   employment_type="REGULAR", source="ERP")
    session.add(emp)
    await session.flush()
    for ym, amt in (("2026-06", 3_600_000), ("2026-07", 3_600_000),
                    ("2026-08", 3_600_000), ("2026-09", 2_400_000)):   # 9월 = 20/30 일할
        await _payroll(session, emp, ym, amt)
    avg = await statutory_avg_wage(session, emp, anchor=emp.resigned_on)
    # 산정기간 6/21~9/20(92일): 6월 10일분 120만 + 7·8월 720만 + 9월 240만 = 1,080만 → ÷92×30
    assert round(avg) == round(10_800_000 / 92 * 30)
    assert avg > 3_400_000                               # 종전 산식(최근 3건 평균)은 320만


async def test_statutory_avg_wage_first_month_hire_and_missing_months(session):
    from erp.hr.retirement_service import statutory_avg_wage
    emp = Employee(employee_no="SCN-R2", name="신규", hired_on=date(2026, 8, 16),
                   job_group="STAFF", employment_type="REGULAR", source="ERP")
    session.add(emp)
    await session.flush()
    await _payroll(session, emp, "2026-08", 1_800_000)                  # 16/31 일할
    await _payroll(session, emp, "2026-09", 3_600_000)
    avg = await statutory_avg_wage(session, emp, anchor=date(2026, 10, 9))   # 10월 급여 미생성
    # 기간 끝 = 급여 있는 마지막 달 말일(9/30), 시작 = 입사일(8/16): 46일·540만
    assert round(avg) == round(5_400_000 / 46 * 30)
    nobody = Employee(employee_no="SCN-R3", name="이력없음", hired_on=date(2026, 1, 1),
                      job_group="STAFF", employment_type="REGULAR", source="ERP")
    session.add(nobody)
    await session.flush()
    assert await statutory_avg_wage(session, nobody, anchor=date(2026, 9, 30)) == 0.0


# ── S-A: 재큐가 worker 신호를 안 보내 5분 폴링까지 NEW로 방치 ───────────────────────────────
async def test_requeue_signals_worker_after_commit(client, session, monkeypatch):
    from erp.integrations import his_webhook_router as hw
    from erp.integrations.models import HisInboundEvent
    calls = []

    async def _fake_signal():
        calls.append(1)
    monkeypatch.setattr(hw, "_signal_inbound", _fake_signal)
    hdr = await auth_as(session, role="ADMIN")
    ev = HisInboundEvent(domain="finance", event_type="billing.adjusted", source_ref="SCN-RQ",
                         payload={}, status="DEAD", retry_count=5, error="x")
    session.add(ev)
    await session.flush()
    r = await client.post(f"{V}/integrations/his/inbound/{ev.id}/requeue", headers=hdr)
    assert r.status_code == 200 and r.json()["status"] == "NEW"
    assert calls == [1]
    await session.refresh(ev)
    assert ev.status == "NEW" and ev.retry_count == 0


# ── 메뉴·권한 정렬: 피폭선량(건강정보)이 전 직원에게 보였다 · 직원 청구 화면 품목 선택 불가 ─────────
async def test_radiation_doses_scoped_by_role(client, session):
    from erp.common.models import User
    from erp.regulatory.models import RadiationDose
    session.add_all([Department(id=2, name="영상의학과"), Department(id=5, name="원무과")])
    a = Employee(employee_no="SCN-RD1", name="종사자A", hired_on=date(2025, 1, 1), job_group="STAFF",
                 employment_type="REGULAR", source="ERP", department_id=2)
    b = Employee(employee_no="SCN-RD2", name="종사자B", hired_on=date(2025, 1, 1), job_group="STAFF",
                 employment_type="REGULAR", source="ERP", department_id=5)
    session.add_all([a, b])
    await session.flush()
    session.add_all([RadiationDose(employee_id=a.id, period="2026-09", dose_msv=1.2, measured_on=date(2026, 9, 30)),
                     RadiationDose(employee_id=b.id, period="2026-09", dose_msv=0.4, measured_on=date(2026, 9, 30))])
    await session.flush()

    async def ids(hdr):
        r = await client.get(f"{V}/regulatory/radiation/doses?year=2026", headers=hdr)
        assert r.status_code == 200, r.text
        return {i["employee_id"] for i in r.json()["items"]}
    assert await ids(await auth_as(session, user_id=11, role="ADMIN")) == {a.id, b.id}
    assert await ids(await auth_as(session, user_id=12, role="MANAGER", dept_id=2)) == {a.id}
    staff = await auth_as(session, user_id=13, role="STAFF", dept_id=5)
    assert await ids(staff) == set()                       # 직원 미연결 계정은 아무것도 못 본다
    (await session.get(User, 13)).employee_id = b.id
    await session.flush()
    assert await ids(staff) == {b.id}                      # 본인 것만


async def test_requestable_items_for_staff_without_item_read(client, session):
    from erp.scm.models import Item
    session.add(Item(code="SCN-I1", name="거즈", unit="EA", category="SUPPLY"))
    await session.flush()
    hdr = await auth_as(session, role="STAFF")
    assert (await client.get(f"{V}/inventory/items", headers=hdr)).status_code == 403
    r = await client.get(f"{V}/inventory/requestable-items", headers=hdr)
    assert r.status_code == 200
    it = next(i for i in r.json()["items"] if i["code"] == "SCN-I1")
    assert set(it) == {"id", "code", "name", "unit", "is_narcotic", "stock", "safety_stock"}


# ── 시나리오 2회차 S-B: ALL 스코프의 잘못된 진료월이 500 ─────────────────────────────────────
@pytest.mark.parametrize("cm", ["2026-1x", "2026-13", "20261-0", "2026-9"])
async def test_calc_from_chart_bad_month_422_for_admin(client, session, cm):
    hdr = await auth_as(session, role="ADMIN")
    r = await client.get(f"{V}/claims/calc/from-chart?chart_no=TEST-1&care_month={cm}", headers=hdr)
    assert r.status_code == 422, r.text


# ── 시나리오 2회차 검토: 청구용 품목이 마약류 재고량을 전 직원에게 노출 · 평균임금 기간이 월말에 하루 앞섬 ──
async def test_requestable_items_hide_narcotic_stock_without_permission(client, session):
    from erp.scm.models import Item
    session.add_all([Item(code="SCN-N1", name="펜타닐", unit="EA", category="DRUG", is_narcotic=True),
                     Item(code="SCN-G1", name="거즈2", unit="EA", category="SUPPLY")])
    await session.flush()

    async def stock(hdr, code):
        r = await client.get(f"{V}/inventory/requestable-items", headers=hdr)
        return next(i for i in r.json()["items"] if i["code"] == code)["stock"]
    staff = await auth_as(session, user_id=21, role="STAFF")
    assert await stock(staff, "SCN-N1") is None
    assert await stock(staff, "SCN-G1") == 0.0
    admin = await auth_as(session, user_id=22, role="ADMIN")
    assert await stock(admin, "SCN-N1") == 0.0


async def test_statutory_avg_wage_window_starts_on_first_of_month(session):
    """end=11/30 → 산정기간 9/1~11/30(91일). 종전은 8/31부터라 8월 일급이 하루 섞였다."""
    from erp.hr.retirement_service import statutory_avg_wage
    emp = Employee(employee_no="SCN-R4", name="말일", hired_on=date(2020, 1, 1),
                   job_group="STAFF", employment_type="REGULAR", source="ERP")
    session.add(emp)
    await session.flush()
    for ym, amt in (("2026-08", 0), ("2026-09", 3_000_000), ("2026-10", 3_000_000),
                    ("2026-11", 3_000_000)):           # 8월 무급(0) — 섞이면 평균이 내려간다
        await _payroll(session, emp, ym, amt)
    avg = await statutory_avg_wage(session, emp, anchor=date(2026, 11, 30))
    assert round(avg) == round(9_000_000 / 91 * 30)


# ── 시나리오 3회차: 청구 심사 전표 수동 역분개 → GL만 바뀌고 결정액은 그대로 ─────────────────────────
async def test_manual_reverse_of_claim_review_voucher_blocked(client, session):
    from erp.finance.models import Account
    from erp.finance.service import VoucherLineDTO, VoucherService
    session.add_all([Account(code="1140", name="보험미수금", kind="ASSET"),
                     Account(code="5910", name="청구삭감손실", kind="EXPENSE")])
    await session.flush()
    v = await VoucherService().create(session, source_module="his.claim", source_ref="claim:review:C-1",
                                      voucher_on=date(2026, 9, 1),
                                      lines=[VoucherLineDTO("5910", "DEBIT", 100),
                                             VoucherLineDTO("1140", "CREDIT", 100)])
    hdr = await auth_as(session, role="ADMIN")
    r = await client.post(f"{V}/finance/vouchers/{v.id}/reverse", json={"reason": "오류"}, headers=hdr)
    assert r.status_code == 422 and "HIS에서 재심사" in r.text


# ── 시나리오 5회차: LIS 상태 폴링 LIKE 미이스케이프 ─────────────────────────────────────────────
async def test_lis_status_poll_does_not_wildcard_match(session, monkeypatch):
    import hashlib
    import hmac
    import time

    from erp.config import get_settings
    from erp.integrations.lis_webhook_router import lis_billing_status
    from erp.integrations.models import HisInboundEvent
    monkeypatch.setattr(get_settings(), "lis_billing_secret", "lis-secret")
    session.add(HisInboundEvent(domain="lis", event_type="lis.billing", source_ref="lis:LXB:1",
                                payload={}, status="DEAD", error="남의 이벤트"))
    await session.flush()
    ts = str(int(time.time()))
    sig = hmac.new(b"lis-secret", f"{ts}.L_B".encode(), hashlib.sha256).hexdigest()
    out = await lis_billing_status("L_B", x_integration_signature=sig, x_integration_ts=ts,
                                   session=session)
    assert out["status"] == "NOT_FOUND"


# ── 시나리오 6회차: HIS 페이로드가 ERP 예약 확인 표지를 실어 확인 절차를 우회 ─────────────────────────
def test_webhook_strips_erp_reserved_payload_keys():
    from erp.integrations.his_webhook_router import WebhookBody
    b = WebhookBody.model_validate({"event": "claim.reviewed", "ref": "claim:review:X:A:1:0", "payload": {
        "claimId": "X", "_erpConfirmedRestore": {"by": 1}, "_erpAnything": 1}})
    assert b.payload == {"claimId": "X"}


async def test_requeue_drops_operator_confirmation(client, session, monkeypatch):
    """확인 표지는 재처리 시 제거 — 옛 확인으로 나중에 복원되지 않게(6회차 검토)."""
    from erp.integrations import his_webhook_router as hw
    from erp.integrations.models import HisInboundEvent

    async def _noop():
        return None
    monkeypatch.setattr(hw, "_signal_inbound", _noop)
    hdr = await auth_as(session, role="ADMIN")
    ev = HisInboundEvent(domain="finance", event_type="claim.reviewed", source_ref="SCN-RQ-C",
                         payload={"claimId": "X", "_erpConfirmedRestore": {"by": 1}}, status="DEAD")
    session.add(ev)
    await session.flush()
    r = await client.post(f"{V}/integrations/his/inbound/{ev.id}/requeue", headers=hdr)
    assert r.status_code == 200
    await session.refresh(ev)
    assert ev.payload == {"claimId": "X"}


async def test_apply_confirmed_only_for_restore_hold(client, session):
    """다른 사유(충돌·옛 가드)로 DEAD된 재심사에 확인을 실으면 이미 이의로 복원된 건이 이중 복원된다(6회차)."""
    from erp.integrations.inbound_consumer import RESTORE_HOLD_PREFIX
    from erp.integrations.models import HisInboundEvent
    hdr = await auth_as(session, role="ADMIN")
    other = HisInboundEvent(domain="finance", event_type="claim.reviewed", source_ref="SCN-AC-1",
                            payload={}, status="DEAD", error="이 청구의 직전 심사 결정 이후 이의신청 종결이…")
    held = HisInboundEvent(domain="finance", event_type="claim.reviewed", source_ref="SCN-AC-2",
                           payload={}, status="DEAD", error="ERP-2003: " + RESTORE_HOLD_PREFIX + "(revision 없음)는…")
    session.add_all([other, held])
    await session.flush()
    r = await client.post(f"{V}/integrations/his/inbound/{other.id}/apply-confirmed",
                          json={"reason": "확인"}, headers=hdr)
    assert r.status_code == 422 and "다른 사유" in r.text
    r = await client.post(f"{V}/integrations/his/inbound/{held.id}/apply-confirmed",
                          json={"reason": "확인"}, headers=hdr)
    assert r.status_code == 200
