"""기관 설정 (common.org_settings) — 단일행 upsert·권한·표기 우선순위(DB>env>폴백)."""
from datetime import date

import pytest

from erp.app.admin_router import OrgSettingsBody, get_org_settings, put_org_settings
from erp.common.org_service import OrgSettingsService, org_display_name
from erp.common.router import CurrentUser
from erp.errors import ForbiddenError, ValidationError

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=1, license_flag=True)
DIRECTOR = CurrentUser(user_id=2, role="DIRECTOR", dept_id=1, license_flag=True)
MANAGER = CurrentUser(user_id=3, role="MANAGER", dept_id=1, license_flag=True)

svc = OrgSettingsService()


async def test_upsert_and_get_singleton(session):
    """단일행 upsert — 최초 생성 후 갱신은 같은 행(id=1). 공백 필드 NULL 정규화."""
    r1 = await svc.upsert(session, actor=1, name="OO의료재단 OO병원",
                          biz_reg_no="123-45-67890", tel="  ")
    assert r1.id == 1 and r1.name == "OO의료재단 OO병원"
    assert r1.biz_reg_no == "123-45-67890" and r1.tel is None    # 공백→NULL
    r2 = await svc.upsert(session, actor=1, name="OO의료재단 신관", representative="김원장")
    assert r2.id == 1 and r2.name == "OO의료재단 신관" and r2.representative == "김원장"
    assert (await svc.get(session)).id == 1


async def test_upsert_requires_name(session):
    with pytest.raises(ValidationError, match="기관 정식명칭"):
        await svc.upsert(session, actor=1, name="  ")


async def test_display_name_precedence_db_over_env(session, monkeypatch):
    """표기 우선순위 — DB 설정 > env ORG_NAME > None(호출부 폴백)."""
    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "org_name", "환경변수병원")
    assert await org_display_name(session) == "환경변수병원"      # DB 없음 → env
    await svc.upsert(session, actor=1, name="DB설정병원")
    assert await org_display_name(session) == "DB설정병원"        # DB 우선
    monkeypatch.setattr(get_settings(), "org_name", "")
    assert await org_display_name(session) == "DB설정병원"


async def test_display_name_none_when_unset(session, monkeypatch):
    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "org_name", "")
    assert await org_display_name(session) is None


async def test_ep_get_put_roles(session, monkeypatch):
    """EP 권한 — 조회 DIRECTOR/ADMIN·저장 ADMIN 전용. source(db/env/unset) 구분."""
    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "org_name", "")
    out = await get_org_settings(user=DIRECTOR, session=session)
    assert out["configured"] is False and out["source"] == "unset"
    with pytest.raises(ForbiddenError):
        await get_org_settings(user=MANAGER, session=session)     # 조회도 관리자급만
    with pytest.raises(ForbiddenError):
        await put_org_settings(body=OrgSettingsBody(name="X"), user=DIRECTOR, session=session)
    saved = await put_org_settings(
        body=OrgSettingsBody(name="OO병원", care_org_no="11100001"),
        user=ADMIN, session=session)
    assert saved["configured"] and saved["source"] == "db" and saved["name"] == "OO병원"
    out2 = await get_org_settings(user=DIRECTOR, session=session)
    assert out2["source"] == "db" and out2["care_org_no"] == "11100001"


async def test_cert_pdf_uses_org_name(monkeypatch):
    """증명서 발급기관 — 기관 설정명이 '{기관명}장'으로 표기(견본 문구 근절 유지)."""
    from types import SimpleNamespace

    import erp.common.pdf as pdf_mod
    from erp.hr.router import _cert_pdf

    captured = {}
    monkeypatch.setattr(pdf_mod, "render_pdf",
                        lambda t, ctx: captured.update(ctx) or b"%PDF-fake")
    emp = SimpleNamespace(name="김민준", employee_no="E0001", department_id=None,
                          position=None, hired_on=date(2020, 1, 1), resigned_on=None)
    issue = SimpleNamespace(id=7, cert_type="EMPLOYMENT", purpose=None)
    _cert_pdf(emp, issue, org_name="OO의료재단 OO병원")
    assert captured["hospital_name"] == "OO의료재단 OO병원장"
    _cert_pdf(emp, issue, org_name=None)
    assert captured["hospital_name"] == "본 의료기관장"


async def test_tax_report_includes_org_header_rows(session):
    """세무 신고서식 xlsx — 기관 설정 시 상호·사업자번호·대표자 헤더행 포함(미설정 시 생략)."""
    import io

    from openpyxl import load_workbook

    from erp.tax.router import tax_report_xlsx

    async def _rows(resp):
        body = b"".join([chunk async for chunk in resp.body_iterator])
        ws = load_workbook(io.BytesIO(body)).active
        return [[c.value for c in r] for r in ws.iter_rows()]

    # 미설정 → 기관 행 없음(견본값 미출력)
    rows0 = await _rows(await tax_report_xlsx(yyyymm="2026-06", user=ADMIN, session=session))
    assert not any(r[0] == "기관" for r in rows0[1:])
    # 설정 → 상호·사업자번호·대표자 행 선두 포함
    await svc.upsert(session, actor=1, name="OO의료재단 OO병원",
                     biz_reg_no="123-45-67890", representative="김원장")
    rows1 = await _rows(await tax_report_xlsx(yyyymm="2026-06", user=ADMIN, session=session))
    org_rows = [r for r in rows1 if r[0] == "기관"]
    assert [r[1] for r in org_rows] == ["상호(법인명)", "사업자등록번호", "대표자"]
    assert org_rows[0][2] == "OO의료재단 OO병원" and org_rows[1][2] == "123-45-67890"


async def test_domain_and_extra_custom_fields(session):
    """기관 설정 확대(#6) — 도메인·기타 커스텀 [{key,value}] 저장(빈 키 제거)."""
    row = await svc.upsert(session, actor=1, name="위루비병원",
                           domain="hospital.example.org",
                           extra=[{"key": "설립일", "value": "2020-01-01"},
                                  {"key": "", "value": "무시"},        # 빈 키 제거
                                  {"key": "대표번호2", "value": "064-800-0000"}])
    assert row.domain == "hospital.example.org"
    assert row.extra == [{"key": "설립일", "value": "2020-01-01"},
                         {"key": "대표번호2", "value": "064-800-0000"}]
    # 빈 리스트 → None
    row2 = await svc.upsert(session, actor=1, name="위루비병원", extra=[])
    assert row2.extra is None


async def test_fiscal_start_month_change_guard(session):
    """결산 전표 존재 시 회계연도 시작월 변경 차단(적대검토 MED/HIGH — 멱등키 기간 정합).

    가드는 앱 계층(admin_router.put_org_settings)에 있음 — common은 finance 의존 금지(import-linter).
    """
    from erp.app.admin_router import OrgSettingsBody, put_org_settings
    from erp.finance.models import Voucher

    await svc.upsert(session, actor=1, name="회계연도병원", fiscal_year_start_month=7)
    # would_change 헬퍼(순수 커널) — 값 변경 여부만 판단
    assert await svc.fiscal_month_would_change(session, 3) is True
    assert await svc.fiscal_month_would_change(session, 7) is False
    assert await svc.fiscal_month_would_change(session, None) is False

    # 결산 없으면 시작월 변경 허용(라우터 경유·신규 값이라 raise 없음)
    await put_org_settings(OrgSettingsBody(name="회계연도병원", fiscal_year_start_month=7),
                           user=ADMIN, session=session)   # 동일 값 → would_change False → 통과
    # 결산 전표 생성 후 시작월 변경(7→3)은 차단(raise는 upsert 전이라 직렬화 미도달)
    session.add(Voucher(voucher_on=date(2025, 12, 31), source_module="closing",
                        source_ref="closing:2025", status="POSTED", memo="결산"))
    await session.flush()
    with pytest.raises(ValidationError, match="연말결산 전표가 존재"):
        await put_org_settings(OrgSettingsBody(name="회계연도병원", fiscal_year_start_month=3),
                               user=ADMIN, session=session)
