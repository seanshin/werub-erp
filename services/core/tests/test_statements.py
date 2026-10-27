"""재무제표·결산 (FR-08) — 시산표·손익계산서·재무상태표."""
from datetime import date

from erp.finance.models import Account
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.statements import StatementService

vs = VoucherService()
ss = StatementService()
D = "DEBIT"
C = "CREDIT"


async def _seed(session):
    session.add_all([
        Account(code="1110", name="현금성자산", kind="ASSET"),
        Account(code="2110", name="미지급금", kind="LIABILITY"),
        Account(code="4110", name="외래수익", kind="REVENUE"),
        Account(code="5110", name="진료비용", kind="EXPENSE"),
    ])
    await session.flush()
    on = date(2026, 5, 10)
    # 수익: 현금 100k / 수익 100k
    await vs.create(session, source_module="manual", source_ref="v1", voucher_on=on,
                    lines=[VoucherLineDTO("1110", D, 100000), VoucherLineDTO("4110", C, 100000)])
    # 현금 비용: 비용 30k / 현금 30k
    await vs.create(session, source_module="manual", source_ref="v2", voucher_on=on,
                    lines=[VoucherLineDTO("5110", D, 30000), VoucherLineDTO("1110", C, 30000)])
    # 외상 비용: 비용 20k / 미지급 20k
    await vs.create(session, source_module="manual", source_ref="v3", voucher_on=on,
                    lines=[VoucherLineDTO("5110", D, 20000), VoucherLineDTO("2110", C, 20000)])
    await session.flush()


async def test_trial_balance_balanced(session):
    await _seed(session)
    tb = await ss.trial_balance(session, since=date(2026, 5, 1), until=date(2026, 5, 31))
    assert tb["total_debit"] == 150000.0 and tb["total_credit"] == 150000.0
    assert tb["balanced"] is True
    cash = next(a for a in tb["accounts"] if a["account_code"] == "1110")
    assert cash["debit"] == 100000.0 and cash["credit"] == 30000.0


async def test_income_statement(session):
    await _seed(session)
    inc = await ss.income_statement(session, since=date(2026, 5, 1), until=date(2026, 5, 31))
    assert inc["total_revenue"] == 100000.0 and inc["total_expense"] == 50000.0
    assert inc["net_income"] == 50000.0
    assert inc["revenues"][0]["name"] == "외래수익"


async def test_balance_sheet(session):
    await _seed(session)
    bs = await ss.balance_sheet(session, as_of=date(2026, 5, 31))
    assert bs["total_assets"] == 70000.0          # 현금 100k−30k
    assert bs["total_liabilities"] == 20000.0     # 미지급
    assert bs["equity"] == 50000.0                # 자산−부채
    assert bs["retained_earnings"] == 50000.0     # 누적순이익 = 자본
    assert bs["balanced"] is True


async def test_period_filter_excludes_other_months(session):
    await _seed(session)
    inc = await ss.income_statement(session, since=date(2026, 6, 1), until=date(2026, 6, 30))
    assert inc["net_income"] == 0.0  # 5월 전표는 6월 손익에 미포함


# ── 현금흐름표 (간접법) ─────────────────────────────────────
async def _seed_cashflow(session):
    session.add_all([
        Account(code="1110", name="현금성자산", kind="ASSET"),
        Account(code="1130", name="미수금", kind="ASSET"),
        Account(code="1710", name="의료장비", kind="ASSET"),
        Account(code="1810", name="감가상각누계액", kind="ASSET"),
        Account(code="2210", name="리스부채", kind="LIABILITY"),
        Account(code="4110", name="외래수익", kind="REVENUE"),
        Account(code="5110", name="진료비용", kind="EXPENSE"),
        Account(code="5210", name="감가상각비", kind="EXPENSE"),
    ])
    await session.flush()
    on = date(2026, 5, 10)
    L = VoucherLineDTO
    await vs.create(session, source_module="manual", source_ref="c1", voucher_on=on,
                    lines=[L("1110", D, 100000), L("4110", C, 100000)])   # 현금매출
    await vs.create(session, source_module="manual", source_ref="c2", voucher_on=on,
                    lines=[L("1130", D, 50000), L("4110", C, 50000)])     # 외상매출(미수금↑)
    await vs.create(session, source_module="manual", source_ref="c3", voucher_on=on,
                    lines=[L("5110", D, 30000), L("1110", C, 30000)])     # 현금비용
    await vs.create(session, source_module="manual", source_ref="c4", voucher_on=on,
                    lines=[L("5210", D, 20000), L("1810", C, 20000)])     # 감가(비현금)
    await vs.create(session, source_module="manual", source_ref="c5", voucher_on=on,
                    lines=[L("1710", D, 40000), L("1110", C, 40000)])     # 자산 현금취득(투자)
    await vs.create(session, source_module="manual", source_ref="c6", voucher_on=on,
                    lines=[L("1110", D, 60000), L("2210", C, 60000)])     # 차입(재무)
    await session.flush()


async def test_cash_flow_statement_three_activities(session):
    """영업/투자/재무 분류 + 비현금가산·운전자본 + 기초·기말현금 정합."""
    await _seed_cashflow(session)
    cf = await ss.cash_flow_statement(session, since=date(2026, 5, 1), until=date(2026, 5, 31))
    # 당기순이익 = 100k+50k−30k−20k = 100k
    assert cf["operating"]["net_income"] == 100000.0
    # 영업 = 순이익 + (미수금↑ −50k) + (감가누계 가산 +20k) = 70k
    assert cf["operating"]["subtotal"] == 70000.0
    assert cf["investing"]["subtotal"] == -40000.0      # 자산취득
    assert cf["financing"]["subtotal"] == 60000.0       # 차입
    assert cf["net_change"] == 90000.0
    assert cf["opening_cash"] == 0.0 and cf["closing_cash"] == 90000.0
    assert cf["reconciled"] is True                      # 기초+순증 == 기말 (항등식)
    assert cf["unmapped_accounts"] == []


async def test_financial_ratios(session):
    """재무비율 — 유동/부채/자기자본비율·인건비율·재료비율 산출."""
    from erp.finance.models import Account
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),       # 유동자산
        Account(code="1710", name="의료장비", kind="ASSET"),   # 비유동자산
        Account(code="2110", name="미지급금", kind="LIABILITY"),  # 유동부채
        Account(code="4110", name="외래수익", kind="REVENUE"),
        Account(code="5310", name="급여비용", kind="EXPENSE"),
        Account(code="5410", name="재료비", kind="EXPENSE"),
    ])
    await session.flush()
    L = VoucherLineDTO
    on = date(2026, 3, 5)
    await vs.create(session, source_module="manual", source_ref="r1", voucher_on=on,
                    lines=[L("1110", D, 200000), L("4110", C, 200000)])   # 수익·현금
    await vs.create(session, source_module="manual", source_ref="r2", voucher_on=on,
                    lines=[L("1710", D, 100000), L("2110", C, 100000)])   # 장비 외상취득
    await vs.create(session, source_module="manual", source_ref="r3", voucher_on=on,
                    lines=[L("5310", D, 60000), L("1110", C, 60000)])     # 인건비
    await vs.create(session, source_module="manual", source_ref="r4", voucher_on=on,
                    lines=[L("5410", D, 40000), L("1110", C, 40000)])     # 재료비
    await session.flush()
    r = await ss.financial_ratios(session, year=2026)
    # 현금 200k−100k=100k(유동자산), 유동부채 100k → 유동비율 1.0
    assert r["current_ratio"] == 1.0
    assert r["debt_ratio"] is not None and r["equity_ratio"] is not None
    assert r["labor_cost_ratio"] == 0.3      # 60k/200k
    assert r["material_cost_ratio"] == 0.2   # 40k/200k
    assert r["operating_margin"] == 0.5      # 순이익 100k/200k


async def test_cash_flow_reconciles_with_unmapped_account(session):
    """미매핑 계정도 영업 폴백+가시화, reconciliation은 항등식으로 보장."""
    await _seed_cashflow(session)
    session.add(Account(code="1190", name="기타자산미분류", kind="OTHER"))
    await session.flush()
    await vs.create(session, source_module="manual", source_ref="c7",
                    voucher_on=date(2026, 5, 12),
                    lines=[VoucherLineDTO("1110", D, 5000), VoucherLineDTO("1190", C, 5000)])
    await session.flush()
    cf = await ss.cash_flow_statement(session, since=date(2026, 5, 1), until=date(2026, 5, 31))
    assert "1190" in cf["unmapped_accounts"]            # 가시화
    assert cf["reconciled"] is True                      # 그래도 정합


async def test_financial_ratios_current_liability_classification(session):
    """유동부채 구분 — 2120 퇴직충당(비유동) 제외·2310 단기차입(유동) 포함 (prefix 버그 회귀).

    수정 전: startswith('21')로 2120을 유동부채로 오분류하고 2310은 누락 → current_ratio 왜곡."""
    session.add_all([
        Account(code="1130", name="미수금", kind="ASSET"),           # 유동자산
        Account(code="2110", name="미지급금", kind="LIABILITY"),      # 유동부채
        Account(code="2120", name="퇴직급여충당부채", kind="LIABILITY"),  # 비유동(제외돼야)
        Account(code="2310", name="단기차입금", kind="LIABILITY"),    # 유동(포함돼야)
        Account(code="2510", name="장기차입금", kind="LIABILITY"),    # 비유동
        Account(code="4110", name="수익", kind="REVENUE"),
        Account(code="1110", name="현금", kind="ASSET"),
    ])
    await session.flush()
    # 유동자산 100(1130) / 유동부채 = 미지급 30(2110) + 단기차입 20(2310) = 50
    # (퇴직충당 40·장기차입 60은 비유동 → 유동부채 제외)
    await vs.create(session, source_module="manual", source_ref="fr1", voucher_on=date(2026, 3, 1),
                    lines=[VoucherLineDTO("1130", D, 100000), VoucherLineDTO("2110", C, 30000),
                           VoucherLineDTO("2310", C, 20000), VoucherLineDTO("2120", C, 40000),
                           VoucherLineDTO("2510", C, 10000)])
    await session.flush()
    r = await ss.financial_ratios(session, year=2026)
    assert r["basis"]["current_liabilities"] == 50000    # 2110+2310 (2120·2510 제외)
    assert r["current_ratio"] == 2.0                      # 100k / 50k


async def test_balance_sheet_surfaces_unmapped_account(session):
    """미매핑(OTHER) 잔액 계정 가시화 + balanced=False (거짓 균형 통과 차단·회귀)."""
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1195", name="미분류자산", kind="OTHER"),        # kind 미매핑
    ])
    await session.flush()
    # 현금↔미분류 이동 — 미분류 계정에 5000 잔액(어떤 kind 버킷에도 안 들어감)
    await vs.create(session, source_module="manual", source_ref="bs1", voucher_on=date(2026, 4, 1),
                    lines=[VoucherLineDTO("1195", D, 5000), VoucherLineDTO("1110", C, 5000)])
    await session.flush()
    bs = await ss.balance_sheet(session, as_of=date(2026, 4, 30))
    assert "1195" in bs["unmapped_accounts"]              # 가시화
    assert bs["balanced"] is False                        # 거짓 균형 통과 차단


async def test_balance_sheet_balanced_with_closing_ifrs_adjustments(session):
    """골든 — 결산조정(closing-ifrs) 전표(대손충당) 포함 시에도 재무상태표 균형·거짓 unmapped 없음."""
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1190", name="대손충당금", kind="ASSET"),      # 차감계정(대변잔액→음수)
        Account(code="4110", name="수익", kind="REVENUE"),
        Account(code="5690", name="대손상각비", kind="EXPENSE"),
    ])
    await session.flush()
    # 정상 수익 + 결산 대손충당(DR 5690 / CR 1190·발생주의 조정)
    await vs.create(session, source_module="manual", source_ref="g1", voucher_on=date(2026, 6, 1),
                    lines=[VoucherLineDTO("1110", D, 100000), VoucherLineDTO("4110", C, 100000)])
    await vs.create(session, source_module="closing-ifrs", source_ref="g2",
                    voucher_on=date(2026, 12, 31),
                    lines=[VoucherLineDTO("5690", D, 8000), VoucherLineDTO("1190", C, 8000)])
    await session.flush()
    bs = await ss.balance_sheet(session, as_of=date(2026, 12, 31))
    assert bs["balanced"] is True and bs["unmapped_accounts"] == []
    # 자산 = 현금 100k − 대손충당 8k = 92k, 자본 = 순이익(수익100k−대손8k)=92k
    assert round(bs["total_assets"]) == 92000 and round(bs["equity"]) == 92000


async def test_statements_exclude_draft_voucher(session):
    """방어선 — 미승인(DRAFT) 전표에 라인이 있어도 재무제표서 배제(status 필터 회귀)."""
    from erp.finance.models import Voucher, VoucherLine
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="4110", name="수익", kind="REVENUE"),
    ])
    await session.flush()
    # POSTED 정상 수익 50k
    await vs.create(session, source_module="manual", source_ref="p1", voucher_on=date(2026, 6, 1),
                    lines=[VoucherLineDTO("1110", D, 50000), VoucherLineDTO("4110", C, 50000)])
    # DRAFT 전표 + 라인 직접 삽입(미승인 금액이 유입되면 안 됨)
    dv = Voucher(voucher_on=date(2026, 6, 2), source_module="manual", source_ref="draft1",
                 status="DRAFT")
    session.add(dv)
    await session.flush()
    session.add_all([VoucherLine(voucher_id=dv.id, account_code="1110", side="DEBIT", amount=99999),
                     VoucherLine(voucher_id=dv.id, account_code="4110", side="CREDIT", amount=99999)])
    await session.flush()
    inc = await ss.income_statement(session, since=date(2026, 6, 1), until=date(2026, 6, 30))
    assert inc["total_revenue"] == 50000        # DRAFT 99999 미포함
    bs = await ss.balance_sheet(session, as_of=date(2026, 6, 30))
    assert round(bs["total_assets"]) == 50000


async def test_cash_flow_includes_fx_deposit_account(session):
    """현금흐름 현금계정 일반화 — 법인 외화예금(1115)도 현금으로 인식(단일 1110 하드코딩 해소).

    수정 전: cash_flow가 1110만 인식 → 1115 유출입이 비현금 계정으로 오분류·현금흐름서 누락.
    수정 후: CorporateAccount(1115)도 현금계정 → 외화예금 순증감이 ΔCash에 반영·reconciled 정확."""
    from erp.finance.models import CorporateAccount
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1115", name="외화예금", kind="ASSET"),
        Account(code="4110", name="수익", kind="REVENUE"),
        CorporateAccount(alias="신한 USD", bank_name="신한", account_no="X",
                         currency="USD", gl_account_code="1115", is_active=True),
    ])
    await session.flush()
    # 원화 현금 수익 50k + 외화예금(1115)으로 직접 수취 30k(환산액)
    await vs.create(session, source_module="manual", source_ref="fx1", voucher_on=date(2026, 5, 5),
                    lines=[VoucherLineDTO("1110", D, 50000), VoucherLineDTO("4110", C, 50000)])
    await vs.create(session, source_module="manual", source_ref="fx2", voucher_on=date(2026, 5, 6),
                    lines=[VoucherLineDTO("1115", D, 30000), VoucherLineDTO("4110", C, 30000)])
    await session.flush()
    cf = await ss.cash_flow_statement(session, since=date(2026, 5, 1), until=date(2026, 5, 31))
    # 기말 현금 = 1110(50k) + 1115(30k) = 80k, 1115가 현금흐름 대상 아니면 누락됐을 것
    assert round(cf["closing_cash"]) == 80000
    assert cf["reconciled"] is True
    assert "1115" not in cf.get("investing", []) and "1115" not in cf["unmapped_accounts"]


async def test_g8_data_driven_classification_overrides_prefix(session):
    """G8 Phase1 — 계정마스터 is_current/cf_activity가 하드코딩 prefix를 오버라이드한다."""
    on = date(2026, 6, 10)
    # 코드 1900: prefix상 자산이지만 비유동(11로 시작 안 함)·cf 폴백=INVESTING.
    # 컬럼으로 유동=TRUE·cf=FINANCING 지정 → 데이터 주도로 재분류돼야 함.
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1900", name="특수자산", kind="ASSET", is_current=True, cf_activity="FINANCING"),
        Account(code="4110", name="수익", kind="REVENUE"),
    ])
    await session.flush()
    # 자산 1900 취득: 1900 50k / 현금 50k
    await vs.create(session, source_module="manual", source_ref="g8-1", voucher_on=on,
                    lines=[VoucherLineDTO("1900", D, 50000), VoucherLineDTO("1110", C, 50000)])
    # 현금 유입(수익): 현금 50k / 수익 50k → ΔCash=0, 1900만 현금흐름 기여
    await vs.create(session, source_module="manual", source_ref="g8-2", voucher_on=on,
                    lines=[VoucherLineDTO("1110", D, 50000), VoucherLineDTO("4110", C, 50000)])
    await session.flush()

    # 유동비율 basis — 1900이 유동자산에 포함(컬럼 TRUE·prefix였다면 제외)
    r = await ss.financial_ratios(session, year=2026)
    assert r["basis"]["current_assets"] == 50000

    # 현금흐름 — 1900(취득 차변 → contrib 음수)이 FINANCING으로 분류(폴백 INVESTING 아님)
    cf = await ss.cash_flow_statement(session, since=date(2026, 6, 1), until=date(2026, 6, 30))
    fin_codes = {i["account_code"] for i in cf["financing"]["items"]}
    inv_codes = {i["account_code"] for i in cf["investing"]["items"]}
    assert "1900" in fin_codes and "1900" not in inv_codes


async def test_dimension_summary_groups_by_cost_center(session):
    """G9 — cost_center 차원별 집계(미지정 라인 제외)."""
    session.add_all([Account(code="1110", name="현금", kind="ASSET"),
                     Account(code="5110", name="비용", kind="EXPENSE")])
    await session.flush()
    on = date(2026, 7, 5)
    await vs.create(session, source_module="manual", source_ref="d1", voucher_on=on,
                    lines=[VoucherLineDTO("5110", D, 3000, cost_center="CC-A"),
                           VoucherLineDTO("1110", C, 3000, cost_center="CC-A")])
    await vs.create(session, source_module="manual", source_ref="d2", voucher_on=on,
                    lines=[VoucherLineDTO("5110", D, 2000, cost_center="CC-B"),
                           VoucherLineDTO("1110", C, 2000)])   # 대변 미지정 → 제외
    await session.flush()
    out = await ss.dimension_summary(session, dim="cost_center",
                                     since=date(2026, 7, 1), until=date(2026, 7, 31))
    m = {i["value"]: i for i in out["items"]}
    assert m["CC-A"]["debit"] == 3000 and m["CC-A"]["credit"] == 3000 and m["CC-A"]["net"] == 0
    assert m["CC-B"]["debit"] == 2000 and m["CC-B"]["net"] == 2000   # 대변 미지정 라인 제외


async def test_equity_changes_uses_fiscal_year_window(session):
    """자본변동계산서가 기관 회계연도 창을 사용(적대검토 HIGH — 달력연도 하드코딩 제거).

    시작월=7이면 FY2025 = 2025-07-01~2026-06-30. 2026-03 수익은 FY2025에 귀속돼야 하고,
    financial_ratios·close_year와 동일 창을 써 net_income 정합이 유지돼야 함.
    """
    from erp.common.org_service import OrgSettingsService
    session.add_all([
        Account(code="1110", name="현금성자산", kind="ASSET"),
        Account(code="3110", name="기본금", kind="CAPITAL"),
        Account(code="4110", name="외래수익", kind="REVENUE"),
    ])
    await session.flush()
    await OrgSettingsService().upsert(session, actor=1, name="회계연도병원",
                                      fiscal_year_start_month=7)
    # 자본 출자(FY2025 시작 직후) + 수익(2026-03, 달력 2026이나 FY2025)
    await vs.create(session, source_module="manual", source_ref="cap", voucher_on=date(2025, 7, 5),
                    lines=[VoucherLineDTO("1110", D, 500000), VoucherLineDTO("3110", C, 500000)])
    await vs.create(session, source_module="manual", source_ref="rev", voucher_on=date(2026, 3, 20),
                    lines=[VoucherLineDTO("1110", D, 80000), VoucherLineDTO("4110", C, 80000)])
    await session.flush()

    eq = await ss.equity_changes(session, fiscal_year=2025)
    # 2026-03 수익이 FY2025 net_income에 잡힘(달력연도였다면 0)
    assert eq["net_income"] == 80000.0
    assert eq["balanced"] is True
    ratios = await ss.financial_ratios(session, year=2025)
    assert ratios["basis"]["net_income"] == 80000.0        # 동일 창 → 정합


async def test_balance_checkpoint_equals_full_scan(session):
    """H-2 자기검증 — 체크포인트+델타 재무상태표가 전구간 스캔과 정확히 일치(정확성 보장)."""
    from datetime import date

    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="2110", name="미지급금", kind="LIABILITY"),
        Account(code="4110", name="수익", kind="REVENUE"),
        Account(code="5110", name="비용", kind="EXPENSE"),
    ])
    await session.flush()
    # 3개월에 걸친 전표
    await vs.create(session, source_module="manual", source_ref="m1a", voucher_on=date(2026, 1, 10),
                    lines=[VoucherLineDTO("1110", D, 1000000), VoucherLineDTO("4110", C, 1000000)])
    await vs.create(session, source_module="manual", source_ref="m1b", voucher_on=date(2026, 1, 20),
                    lines=[VoucherLineDTO("5110", D, 300000), VoucherLineDTO("2110", C, 300000)])
    await vs.create(session, source_module="manual", source_ref="m2a", voucher_on=date(2026, 2, 15),
                    lines=[VoucherLineDTO("1110", D, 500000), VoucherLineDTO("4110", C, 500000)])
    await vs.create(session, source_module="manual", source_ref="m3a", voucher_on=date(2026, 3, 5),
                    lines=[VoucherLineDTO("5110", D, 200000), VoucherLineDTO("1110", C, 200000)])
    await session.flush()

    # 폴백(체크포인트 없음)과 전구간이 동일한지(회귀 없음) — verify
    v0 = await ss.verify_balance_checkpoint(session, as_of=date(2026, 3, 31))
    assert v0["ok"] is True

    # 1월말 체크포인트 적재 → 2·3월은 델타로
    await ss.refresh_balance_checkpoint(session, through=date(2026, 1, 31))
    await session.flush()
    for asof in (date(2026, 2, 1), date(2026, 2, 20), date(2026, 3, 5), date(2026, 3, 31)):
        v = await ss.verify_balance_checkpoint(session, as_of=asof)
        assert v["ok"] is True, (asof, v["mismatched"])
    # balance_sheet(체크포인트 경유)가 정확한 자본 산출(자산-부채)
    bs = await ss.balance_sheet(session, as_of=date(2026, 3, 31))
    # 현금 1000+500-200=1300k·미지급 300k·수익 1500k·비용 500k → 자산1300 부채300 자본1000
    assert bs["total_assets"] == 1300000 and bs["total_liabilities"] == 300000
    assert bs["equity"] == 1000000 and bs["balanced"] is True

    # 증분 체크포인트 — 2월말 추가(1월말 기반) 후에도 정합
    r = await ss.refresh_balance_checkpoint(session, through=date(2026, 2, 28))
    await session.flush()
    assert r["incremental"] is True
    assert (await ss.verify_balance_checkpoint(session, as_of=date(2026, 3, 31)))["ok"] is True
