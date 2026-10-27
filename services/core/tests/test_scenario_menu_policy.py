"""2026-10-09 프런트 시나리오 크롤 — 메뉴 노출 vs 서버 권한 불일치 회귀.

불변식: 메뉴가 어떤 역할에 화면을 보여주면, 그 화면의 주 데이터 권한(MENU_READ_SCOPES)이 그 역할에
NONE이 아니어야 한다(빈 화면/403만 남는 메뉴 금지). + 결재선 단계 적격자의 전표 단건 열람.
"""
from datetime import date

import pytest

from erp.common.menu_service import MENU_CATALOG, MENU_READ_SCOPES, MenuService
from erp.common.policy import POLICY, Role, ScopeKind, resolve_scope
from erp.finance.models import Account
from erp.finance.service import VoucherLineDTO, VoucherService
from tests._http import auth_as, make_user

_CATALOG = {h: roles for _g, items in MENU_CATALOG for h, _l, roles in items}


def test_read_scope_annotations_point_at_real_entries():
    """주석이 오타·구 경로·미등록 정책을 가리키면 불변식 검사가 조용히 무력화된다."""
    for href, (resource, action) in MENU_READ_SCOPES.items():
        assert href in _CATALOG, f"카탈로그에 없는 메뉴: {href}"
        assert (resource, action) in POLICY, f"{href}: 정책 미등록 {(resource, action)}"


@pytest.mark.parametrize("href", sorted(MENU_READ_SCOPES))
def test_menu_roles_have_primary_data_permission(href):
    resource, action = MENU_READ_SCOPES[href]
    for role in _CATALOG[href]:
        scope = resolve_scope(resource, action, role=Role(role), dept_id=1, user_id=1)
        assert scope.kind != ScopeKind.NONE, (
            f"{href} 메뉴가 {role}에 노출되지만 {resource}:{action} 권한이 없음 → 빈 화면/403")


async def test_crawl_findings_menu_visibility(session):
    """크롤 발견 항목 — 역할별 메뉴에서 빠졌는지(정책 쪽은 위 불변식이 강제)."""
    svc = MenuService()

    async def hrefs(role):
        return {it["href"] for g in (await svc.menu_for(session, role=role))["groups"]
                for it in g["items"]}

    staff, mgr, director = await hrefs("STAFF"), await hrefs("MANAGER"), await hrefs("DIRECTOR")
    assert not ({"/patients/closing", "/patients/reservations", "/patients"} & staff)
    assert {"/patients", "/patients/reservations"} <= mgr and "/patients/closing" not in mgr
    assert {"/patients/closing", "/patients", "/hr/incentive"} <= director
    for h in ("/hr/employment-contracts", "/hr/incentive", "/hr/retirement",
              "/hr/medical-contracts", "/hr/statutory"):
        assert h not in mgr, h
    assert "/inventory/workstation" not in director and "/inventory/workstation" in mgr
    # 전 직원 셀프서비스(자재청구·VOC·안전보고)는 그대로
    assert {"/inventory-material-requests", "/patients/voc", "/regulatory/safety"} <= staff


# ── 결재선 단계 적격자의 전표 단건 열람 ─────────────────────────────
async def _pending_voucher_with_rule(session, *roles):
    from erp.common.models import ApprovalRule
    session.add_all([Account(code="1110", name="현금", kind="ASSET"),
                     Account(code="4110", name="외래수익", kind="REVENUE")])
    session.add(ApprovalRule(doc_type="VOUCHER", min_amount=0, active=True,
                             steps=[{"type": "ROLE", "value": r} for r in roles]))
    await session.flush()
    vs = VoucherService()
    v = await vs.create_draft(session, source_ref="menu-pol-1", voucher_on=date(2026, 6, 1),
                              lines=[VoucherLineDTO("1110", "DEBIT", 1000),
                                     VoucherLineDTO("4110", "CREDIT", 1000)], created_by=1)
    await vs.submit(session, voucher_id=v.id, actor=1)
    return v


async def test_step_approver_can_open_that_voucher_only(session, client):
    await make_user(session, user_id=1, role="FINANCE_MANAGER")
    hm = await auth_as(session, user_id=2, role="MANAGER")
    hs = await auth_as(session, user_id=4, role="STAFF")
    v = await _pending_voucher_with_rule(session, "MANAGER", "DIRECTOR")

    r = await client.get(f"/api/v1/finance/vouchers/{v.id}", headers=hm)
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == 1000 and r.json()["status"] == "PENDING"
    r = await client.get(f"/api/v1/finance/vouchers/{v.id}/lines", headers=hm)
    assert r.status_code == 200, r.text
    assert r.json()["draft"] is True and len(r.json()["items"]) == 2
    # 원장 목록은 여전히 막힘 — 결재자에게 원장 전체를 여는 것이 아니다
    assert (await client.get("/api/v1/finance/vouchers", headers=hm)).status_code == 403
    # 단계 비적격자(STAFF)는 단건도 403
    assert (await client.get(f"/api/v1/finance/vouchers/{v.id}", headers=hs)).status_code == 403
    assert (await client.get(f"/api/v1/finance/vouchers/{v.id}/lines",
                             headers=hs)).status_code == 403

    # 1단계 승인 후 MANAGER는 더 이상 현재 단계 적격자가 아니다 → 단건도 닫힘
    assert (await client.post(f"/api/v1/finance/vouchers/{v.id}/approve",
                              headers=hm)).status_code == 200
    assert (await client.get(f"/api/v1/finance/vouchers/{v.id}", headers=hm)).status_code == 403


async def test_voucher_detail_without_rule_keeps_permission_gate(session, client):
    """결재선 없는 전표 — 종전대로 finance.vouchers read만(MANAGER 403·ADMIN 200·없는 전표 404)."""
    await make_user(session, user_id=1, role="FINANCE_MANAGER")
    hm = await auth_as(session, user_id=2, role="MANAGER")
    ha = await auth_as(session, user_id=5, role="ADMIN")
    session.add_all([Account(code="1110", name="현금", kind="ASSET"),
                     Account(code="4110", name="외래수익", kind="REVENUE")])
    await session.flush()
    v = await VoucherService().create_draft(
        session, source_ref="menu-pol-2", voucher_on=date(2026, 6, 1),
        lines=[VoucherLineDTO("1110", "DEBIT", 700), VoucherLineDTO("4110", "CREDIT", 700)],
        created_by=1)
    assert (await client.get(f"/api/v1/finance/vouchers/{v.id}", headers=hm)).status_code == 403
    r = await client.get(f"/api/v1/finance/vouchers/{v.id}", headers=ha)
    assert r.status_code == 200 and r.json()["amount"] == 700
    assert (await client.get("/api/v1/finance/vouchers/999999", headers=ha)).status_code == 404
