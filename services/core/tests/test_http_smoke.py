"""HTTP 계층 스모크 — DI·인증·RBAC·Pydantic 422를 실제 앱으로 검증(P1-1).

서비스 직호출 테스트(1200+건)가 못 잡던 라우터-계약 회귀를 방지. 이번 분기 prod escape 2건:
(1) 2FA 전면 500 — 라우터가 auth_complete(credential=)로 호출, 시그니처는 assertion=(v1.71 수정).
(2) admin billing 4EP가 admin.audit read로 게이트(GL/마스터 mutation인데)—admin.staging write로(v1.71).
둘 다 HTTP 계층 테스트였다면 즉시 적발됐다.
"""

from tests._http import auth_as, bearer

V = "/api/v1"


async def test_unauthenticated_401(client):
    """토큰 없으면 401 — 인증 dependency가 실제로 동작(서비스 직호출은 우회)."""
    r = await client.get(f"{V}/finance/vouchers")
    assert r.status_code == 401


async def test_finance_vouchers_readable_by_admin(client, session):
    hdr = await auth_as(session, role="ADMIN")
    r = await client.get(f"{V}/finance/vouchers", headers=hdr)
    assert r.status_code == 200
    assert "items" in r.json()


async def test_finance_write_forbidden_for_staff(client, session):
    """RBAC 403 — finance.vouchers write는 STAFF 불가(유효 body라 검증 통과 후 스코프에서 차단)."""
    hdr = await auth_as(session, role="STAFF")
    body = {"voucher_on": "2026-06-01", "memo": "t",
            "lines": [{"account_code": "1110", "side": "DEBIT", "amount": 100},
                      {"account_code": "4110", "side": "CREDIT", "amount": 100}]}
    r = await client.post(f"{V}/finance/vouchers", json=body, headers=hdr)
    assert r.status_code == 403


async def test_voucher_post_validation_422(client, session):
    """Pydantic 422 — 필수 필드 누락. 서비스 직호출은 이 검증 계층을 건너뜀."""
    hdr = await auth_as(session, role="ADMIN")
    r = await client.post(f"{V}/finance/vouchers", json={"memo": "no lines/date"}, headers=hdr)
    assert r.status_code == 422


async def test_admin_billing_backfill_write_gated(client, session):
    """escape 회귀 — admin billing 백필(GL/마스터 mutation)은 write 게이트. STAFF 차단(과거 read 게이트)."""
    hdr = await auth_as(session, role="STAFF")
    r = await client.post(
        f"{V}/admin/integrations/billing/backfill?from_on=2026-06-01&to_on=2026-06-30",
        headers=hdr)
    assert r.status_code == 403


async def test_2fa_complete_no_500_regression(client, session, monkeypatch):
    """escape 회귀 — 2FA 완료가 kwarg 불일치로 전면 500 나던 것. 잘못된 토큰은 4xx(500 아님)."""
    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "pin_2fa_enabled", True, raising=False)
    r = await client.post(f"{V}/auth/2fa/complete",
                          json={"token": "garbage-token", "credential": {}})
    # 핵심: 500(TypeError)이 아니어야 한다. 유효하지 않은 토큰이므로 4xx.
    assert r.status_code != 500
    assert 400 <= r.status_code < 500


async def test_workstation_kiosk_denied_regulatory_safety(client, session):
    """escape 회귀(v1.71) — WORKSTATION kiosk는 규제·환자안전 접근 불가. 토큰만으론 통과 못함."""
    # WORKSTATION은 User 행이 아니라 WorkstationPass 검증 → 미시딩이면 인증 자체가 막힘(폐기/만료).
    hdr = bearer(user_id=999, role="WORKSTATION")
    r = await client.get(f"{V}/regulatory/safety/incidents", headers=hdr)
    assert r.status_code in (401, 403, 404)   # 통과(200) 아니어야 — kiosk 과다부여 회귀 방지


async def test_health_shallow_ok(client):
    r = await client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_metrics_prometheus_text(client, session):
    """P2-3 — /api/metrics 무인증 200·Prometheus 텍스트. 요청 계측 후 카운터 노출 확인."""
    await auth_as(session, role="ADMIN")
    # 계측 대상 요청 1건 발생시킨 뒤 스크레이프
    await client.get("/api/health")
    r = await client.get("/api/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "erp_up 1" in body
    assert "erp_http_requests_total" in body
    assert "erp_db_up" in body
    # 자기계측 제외 — /api/metrics 경로는 카운터에 없어야
    assert 'path="/api/metrics"' not in body


async def test_list_suppliers_dropdown_source(client, session):
    """공급사 목록 EP — 위탁재고·발주 화면 드롭다운 소스. scm.po read 게이트·활성만 기본."""
    from erp.scm.models import Supplier
    session.add_all([Supplier(name="가나약품", business_no="1112223330", is_active=True),
                     Supplier(name="폐업상사", business_no="1112223331", is_active=False)])
    await session.commit()
    hdr = await auth_as(session, role="LOGISTICS_MANAGER")
    r = await client.get("/api/v1/inventory/suppliers", headers=hdr)
    assert r.status_code == 200
    names = [i["name"] for i in r.json()["items"]]
    assert "가나약품" in names and "폐업상사" not in names   # 활성만(기본)
    # 거래처 관리 화면 필드(v1.202) — contact·is_active·vendor_type·eval_score 포함
    row = next(i for i in r.json()["items"] if i["name"] == "가나약품")
    assert row["is_active"] is True and "contact" in row and "eval_score" in row
    # active_only=false — 비활성 포함(관리 화면 소스)
    r = await client.get("/api/v1/inventory/suppliers?active_only=false", headers=hdr)
    assert "폐업상사" in [i["name"] for i in r.json()["items"]]


async def test_list_suppliers_forbidden_for_staff(client, session):
    hdr = await auth_as(session, role="STAFF")
    r = await client.get("/api/v1/inventory/suppliers", headers=hdr)
    assert r.status_code == 403


async def test_returnable_lots_scoped_to_po_items(client, session):
    """RMA 로트 선택 소스 — 발주 품목·공급사·잔량>0 로트만. 반품은 lot_id 지정이라 이 EP로 로트 노출."""
    from datetime import date
    from erp.scm.models import Item, Lot, PoLine, PurchaseOrder, Supplier
    sup = Supplier(name="반품테스트상사", business_no="2223334440")
    it1 = Item(code="RMA1", name="반품품목", category="SUPPLY", is_narcotic=False, safety_stock_qty=0)
    it2 = Item(code="RMA2", name="타품목", category="SUPPLY", is_narcotic=False, safety_stock_qty=0)
    session.add_all([sup, it1, it2])
    await session.flush()
    po = PurchaseOrder(supplier_id=sup.id, status="RECEIVED", requested_by=1)
    session.add(po)
    await session.flush()
    session.add(PoLine(po_id=po.id, item_id=it1.id, qty=10, unit_price=5000, received_qty=10))
    # 발주 품목(it1) 잔량 로트 → 노출 · 타품목(it2) 로트 → 제외
    session.add_all([
        Lot(item_id=it1.id, lot_no="L-A", expiry_on=date(2027, 1, 1), received_on=date(2026, 5, 1),
            received_qty=10, remaining_qty=6, unit_cost=5000, supplier_id=sup.id),
        Lot(item_id=it2.id, lot_no="L-B", expiry_on=date(2027, 1, 1), received_on=date(2026, 5, 1),
            received_qty=5, remaining_qty=5, unit_cost=3000, supplier_id=sup.id)])
    await session.commit()
    hdr = await auth_as(session, role="LOGISTICS_MANAGER")
    r = await client.get(f"/api/v1/inventory/purchase-orders/{po.id}/returnable-lots", headers=hdr)
    assert r.status_code == 200
    lots = r.json()["items"]
    assert [x["lot_no"] for x in lots] == ["L-A"]      # 발주 품목만·잔량>0
    assert lots[0]["remaining_qty"] == 6 and lots[0]["item_name"] == "반품품목"


def test_api_datetime_encoder_kst():
    """전역 직렬화 — PG timestamptz(UTC aware)는 KST(+09:00)로, naive는 그대로(v1.259.1 광역감사).

    미등록 시 raw ORM datetime이 +00:00으로 나가 FE 문자열 절단 표시가 전 화면 -9h 왜곡."""
    from datetime import datetime, timezone

    from erp.common.kst import api_datetime_encoder
    aware = datetime(2026, 7, 27, 23, 7, 36, tzinfo=timezone.utc)   # = KST 07-28 08:07:36
    assert api_datetime_encoder(aware) == "2026-07-28T08:07:36+09:00"
    naive = datetime(2026, 7, 28, 8, 7, 36)
    assert api_datetime_encoder(naive) == "2026-07-28T08:07:36"


async def test_jsonable_encoder_registered_via_create_app(client):
    """create_app이 fastapi ENCODERS_BY_TYPE에 KST 인코더 등록 — jsonable_encoder 전역 반영."""
    from datetime import datetime, timezone

    from fastapi.encoders import jsonable_encoder
    out = jsonable_encoder({"t": datetime(2026, 7, 27, 23, 0, tzinfo=timezone.utc)})
    assert out["t"] == "2026-07-28T08:00:00+09:00"


async def test_draft_voucher_shows_amount_and_lines(client, session):
    """승인 대기 전표의 금액·분개가 화면에 보여야 한다(감사 — UI 미노출 데이터).

    M2 maker-checker는 승인 시점에만 전기하므로 DRAFT/PENDING은 VoucherLine이 없다.
    목록 금액을 차변 합으로만 계산하고 분개 EP가 VoucherLine만 조회하던 탓에, **정작 승인
    대상 전표가 0원·빈 표**로 떠 승인자가 금액을 모른 채 결재하는 구조였다.
    """
    hdr = await auth_as(session, role="ADMIN")
    body = {"voucher_on": "2026-06-01", "memo": "초안 전표",
            "lines": [{"account_code": "1110", "side": "DEBIT", "amount": 250_000},
                      {"account_code": "4110", "side": "CREDIT", "amount": 250_000}]}
    created = await client.post(f"{V}/finance/vouchers", json=body, headers=hdr)
    assert created.status_code in (200, 201), created.text
    vid = created.json()["id"]

    lst = (await client.get(f"{V}/finance/vouchers", headers=hdr)).json()
    row = next(i for i in lst["items"] if i["id"] == vid)
    assert row["status"] == "DRAFT"
    assert row["amount"] == 250_000            # 구 동작: 0원
    assert "approved_by" in row and "reject_reason" in row   # 통제 증적

    lines = (await client.get(f"{V}/finance/vouchers/{vid}/lines", headers=hdr)).json()
    assert lines["draft"] is True and len(lines["items"]) == 2   # 구 동작: 빈 표
    assert lines["items"][0]["side"] == "DEBIT"                  # 차변 먼저
