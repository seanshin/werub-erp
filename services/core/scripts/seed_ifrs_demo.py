"""IFRS 측정엔진 데모 시드 — 합성 source 데이터로 ~92% IFRS 측정기능을 시연 가능하게.

배경: seed_synthetic.py는 환자·직원·수가·재고만 시드한다. 그 결과 IFRS 측정엔진(대손/저가법/
이연법인세/재평가/외화/무형/특수관계자/우발/퇴직DBO)은 source 데이터가 없어 preview/post가
전부 0을 반환 → 구현된 ~92% IFRS 부합 기능이 데모 환경에서 시연 불가능했다. 본 스크립트는 각
엔진이 0이 아닌 결과를 내도록 (1) 관련 GL 계정 차트 (2) 설정값(대손율·세율·보험수리가정)
(3) 합성 source 데이터(미수금 연령·재고 NRV·고정/무형자산·외화계좌·특수관계자·우발) 를 적재하고,
데모 기간 결산조정을 IFRS 순서(손상→대손→재고→외화→퇴직재측정→이연법인세)로 전기한다.

원칙:
  - 모든 마스터 레코드는 TEST- 식별(제거 가능). 전표는 (source_module, source_ref) 멱등 → 재실행 안전.
  - ⚠ 운영 DB에 실행하면 GL에 결산조정 전표가 들어가 재무제표에 반영된다 — 데모/파일럿 전용.
    제거 SQL은 본 파일 말미 주석 참조.
  - 계정/설정/마스터는 존재검사로 멱등, 엔진 전기는 source_ref로 멱등.

실행:  services/core 에서  .venv/bin/python scripts/seed_ifrs_demo.py
대상:  DATABASE_URL (기본 dev compose PG) — 운영 적재는 의도적 결정 시에만.
"""
import asyncio
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ── 데모 기간(당월) ─────────────────────────────────────────────
_TODAY = date.today()
YM = _TODAY.strftime("%Y-%m")        # 예: 2026-06
YEAR = _TODAY.year

# ── IFRS 측정에 쓰이는 계정과목 차트(존재검사 멱등) ─────────────
ACCOUNTS: list[tuple[str, str, str]] = [
    ("1110", "현금성자산", "ASSET"),
    ("1115", "외화예금", "ASSET"),
    ("1130", "미수금", "ASSET"),
    ("1150", "재고자산", "ASSET"),
    ("1159", "재고자산평가충당금", "ASSET"),
    ("1180", "이연법인세자산", "ASSET"),
    ("1190", "대손충당금", "ASSET"),
    ("1710", "의료장비", "ASSET"),
    ("1810", "감가상각누계", "ASSET"),
    ("1815", "손상차손누계", "ASSET"),
    ("1850", "무형자산", "ASSET"),
    ("1855", "무형자산상각누계", "ASSET"),
    ("1856", "무형자산손상누계", "ASSET"),
    ("2110", "미지급금", "LIABILITY"),
    ("2120", "퇴직급여충당부채", "LIABILITY"),
    ("2260", "이연법인세부채", "LIABILITY"),
    ("3300", "이익잉여금", "CAPITAL"),
    ("3400", "재평가잉여금", "CAPITAL"),
    ("3410", "보험수리적손익", "CAPITAL"),
    ("4110", "진료수익", "REVENUE"),
    ("4640", "유형자산손상차손환입", "REVENUE"),
    ("4641", "무형자산손상환입", "REVENUE"),
    ("4650", "외화환산이익", "REVENUE"),
    ("5210", "감가상각비", "EXPENSE"),
    ("5215", "무형자산상각비", "EXPENSE"),
    ("5216", "무형자산손상차손", "EXPENSE"),
    ("5320", "퇴직급여비용", "EXPENSE"),
    ("5525", "정보시스템운영비", "EXPENSE"),
    ("5540", "재고자산평가손실", "EXPENSE"),
    ("5620", "유형자산손상차손", "EXPENSE"),
    ("5670", "외화환산손실", "EXPENSE"),
    ("5690", "대손상각비", "EXPENSE"),
    ("5810", "법인세비용", "EXPENSE"),
]

ALLOWANCE_RATES = [
    ("current", 0.005), ("1-30", 0.02), ("31-60", 0.10), ("61-90", 0.30), ("90+", 0.50),
]


async def _seed_masters(session) -> dict:
    """계정·설정·합성 source 마스터 적재(존재검사 멱등). 반환: 생성한 참조 id."""
    from sqlalchemy import func, select

    from erp.finance.account_service import AccountService
    from erp.finance.asset_service import AssetService
    from erp.finance.intangible_service import IntangibleAssetService
    from erp.finance.models import (
        Account, AllowanceRate, Contingency, CorporateAccount,
        RelatedParty, RelatedPartyTxn, Receivable, TaxRateSetting,
    )
    from erp.finance.service import VoucherLineDTO, VoucherService
    from erp.finance.settlement_service import SettlementService
    from erp.hr.models import ActuarialAssumption, Employee, PayrollLine, PayrollRecord
    from erp.scm.models import Item, Lot

    refs: dict = {}

    # 1) 계정 차트
    accts = AccountService()
    existing = set((await session.execute(select(Account.code))).scalars().all())
    made = 0
    for code, name, kind in ACCOUNTS:
        if code not in existing:
            await accts.create(session, code=code, name=name, kind=kind)
            made += 1
    print(f"  계정과목: 신규 {made} / 기존 {len(existing)}")

    # 2) 설정값 — 대손율·법인세율·보험수리가정
    have_rate = set((await session.execute(
        select(AllowanceRate.bucket).where(AllowanceRate.kind == "AR"))).scalars().all())
    for bucket, rate in ALLOWANCE_RATES:
        if bucket not in have_rate:
            session.add(AllowanceRate(kind="AR", bucket=bucket, rate=rate))
    if not (await session.execute(select(func.count()).select_from(TaxRateSetting))).scalar_one():
        session.add(TaxRateSetting(rate=0.20, valid_from=date(YEAR, 1, 1)))
    if not (await session.execute(
            select(func.count()).select_from(ActuarialAssumption))).scalar_one():
        session.add(ActuarialAssumption(discount_rate=0.04, salary_growth=0.03,
                                        retirement_age=60, valid_from=date(YEAR, 1, 1)))
    print("  설정값: 대손율 5버킷 · 법인세율 20% · 보험수리(할인4%/임금상승3%/정년60)")

    # 3) 미수금 — 연령 버킷 분산(대손 ECL source)
    if not (await session.execute(
            select(func.count()).select_from(Receivable)
            .where(Receivable.party.like("TEST-%")))).scalar_one():
        sett = SettlementService()
        plan = [("TEST-보험공단", 40_000_000, 5),    # current
                ("TEST-보험사A", 12_000_000, 20),    # 1-30
                ("TEST-환자수납B", 8_000_000, 50),    # 31-60
                ("TEST-거래처C", 5_000_000, 75),      # 61-90
                ("TEST-장기연체D", 6_000_000, 130)]   # 90+
        for party, amount, days in plan:
            await sett.record_receivable(
                session, party=party, amount=amount,
                billed_on=_TODAY - timedelta(days=days + 10),
                due_on=_TODAY - timedelta(days=days), revenue_account="4110")
        print("  미수금: 5건(current~90+ 버킷, 총 71,000,000)")
    else:
        print("  미수금: 기존 TEST 미수금 존재 — 생략")

    # 4) 재고 저가법 — NRV < 원가 품목
    code = "TEST-NRV-001"
    item = (await session.execute(select(Item).where(Item.code == code))).scalar_one_or_none()
    if item is None:
        item = Item(code=code, name="TEST 저가법 의약품", category="DRUG",
                    nrv_unit=50_000, cost_method="FIFO", safety_stock_qty=10)
        session.add(item)
        await session.flush()
        session.add(Lot(item_id=item.id, lot_no="TEST-LOT-NRV",
                        expiry_on=_TODAY + timedelta(days=180), received_on=_TODAY,
                        received_qty=100, remaining_qty=80, unit_cost=60_000))
        print("  재고: TEST 품목 1종(원가 60,000 > NRV 50,000, 잔량 80 → 평가손실 800,000)")
    else:
        print("  재고: TEST NRV 품목 존재 — 생략")

    # 5) 고정자산 — 재평가용 CT, 손상용 MRI
    asvc = AssetService()

    async def _asset(asset_no, name, cost, life):
        from erp.finance.models import FixedAsset
        a = (await session.execute(
            select(FixedAsset).where(FixedAsset.asset_no == asset_no))).scalar_one_or_none()
        if a is None:
            a = await asvc.register(session, asset_no=asset_no, name=name, category="MEDICAL",
                                    acquired_on=date(YEAR - 2, 6, 1), acquisition_cost=cost,
                                    useful_life_years=life, salvage_value=cost * 0.1)
        return a

    ct = await _asset("TEST-ASSET-CT", "TEST CT 스캐너", 500_000_000, 8)
    mri = await _asset("TEST-ASSET-MRI", "TEST MRI 장비", 800_000_000, 10)
    refs["ct_id"], refs["mri_id"] = ct.id, mri.id
    print("  고정자산: CT(재평가 대상)·MRI(손상 대상) 2종")

    # 6) 무형자산 — EMR 라이선스(상각 대상)
    from erp.finance.models import IntangibleAsset
    soft = (await session.execute(
        select(IntangibleAsset).where(IntangibleAsset.asset_no == "TEST-SOFT-EMR"))
    ).scalar_one_or_none()
    if soft is None:
        res = await IntangibleAssetService().create(
            session, asset_no="TEST-SOFT-EMR", name="TEST EMR 라이선스",
            acquired_on=date(YEAR - 1, 1, 1), cost=120_000_000, useful_life_years=5)
        refs["soft_id"] = res["id"]
    else:
        refs["soft_id"] = soft.id
    print("  무형자산: EMR 라이선스 1종(상각 대상)")

    # 7) 외화계좌 + 초기 외화예금 전표(환산 source) — $100,000 @1,200 부킹
    acct = (await session.execute(
        select(CorporateAccount).where(CorporateAccount.account_no == "TEST-USD-0001"))
    ).scalar_one_or_none()
    if acct is None:
        acct = CorporateAccount(alias="TEST 신한 USD", bank_name="TEST신한은행",
                                account_no="TEST-USD-0001", currency="USD",
                                gl_account_code="1115", is_default=True, holder="TEST법인")
        session.add(acct)
        await session.flush()
        # 초기 외화예금 부킹(1115 차 / 1110 대) — 이후 기말환율로 환산차 발생
        await VoucherService().create(
            session, source_module="ifrs-demo", source_ref="ifrs-demo-fx-deposit",
            voucher_on=_TODAY.replace(day=1), memo="TEST 외화예금 초기 부킹($100,000 @1,200)",
            lines=[VoucherLineDTO("1115", "DEBIT", 120_000_000),
                   VoucherLineDTO("1110", "CREDIT", 120_000_000)])
        print("  외화: USD 계좌 + 초기예금 120,000,000(@1,200) → 기말 @1,300 환산차 +10,000,000")
    else:
        print("  외화: TEST USD 계좌 존재 — 생략")
    refs["fx_acct_id"] = acct.id

    # 8) 특수관계자 + 거래(공시 1024)
    if not (await session.execute(
            select(func.count()).select_from(RelatedParty)
            .where(RelatedParty.name.like("TEST-%")))).scalar_one():
        rp = RelatedParty(name="TEST-이사 김원장", relation="주요경영진")
        session.add(rp)
        await session.flush()
        session.add(RelatedPartyTxn(party_id=rp.id, period=str(YEAR), txn_type="보수",
                                    amount=180_000_000, balance=0))
        session.add(RelatedPartyTxn(party_id=rp.id, period=str(YEAR), txn_type="대여",
                                    amount=50_000_000, balance=50_000_000))
        print("  특수관계자: 1인 + 거래 2건(보수·대여)")
    else:
        print("  특수관계자: 기존 TEST 존재 — 생략")

    # 9) 우발부채(공시 1037)
    if not (await session.execute(
            select(func.count()).select_from(Contingency)
            .where(Contingency.description.like("TEST %")))).scalar_one():
        session.add(Contingency(period=str(YEAR), kind="LIABILITY",
                                description="TEST 의료분쟁 소송(계류중)", estimate=70_000_000,
                                likelihood="중간", status="OPEN"))
        print("  우발부채: 1건(소송 계류, 추정 70,000,000)")
    else:
        print("  우발부채: 기존 TEST 존재 — 생략")

    # 10) 급여(EARNING) — 퇴직 DBO source. 기존 직원 일부에 데모 기간 급여 생성
    emps = (await session.execute(
        select(Employee).where(Employee.resigned_on.is_(None)).limit(10))).scalars().all()
    seeded_pay = 0
    for emp in emps:
        has = (await session.execute(
            select(func.count()).select_from(PayrollRecord)
            .where(PayrollRecord.employee_id == emp.id, PayrollRecord.yyyymm == YM))
        ).scalar_one()
        if has:
            continue
        pr = PayrollRecord(employee_id=emp.id, yyyymm=YM)
        session.add(pr)
        await session.flush()
        session.add(PayrollLine(record_id=pr.id, item_code="BASE", item_kind="EARNING",
                                amount=3_500_000))
        seeded_pay += 1
    refs["payroll_count"] = seeded_pay
    print(f"  급여: EARNING {seeded_pay}건(퇴직충당·DBO source){'' if emps else ' — 직원 없음(seed_synthetic 먼저)'}")
    return refs


async def _run_engines(session, refs: dict) -> None:
    """IFRS 결산조정 전기(순서: 손상→대손→재고→외화→퇴직→이연법인세). 전부 source_ref 멱등."""
    from erp.finance.allowance_service import AllowanceService
    from erp.finance.asset_service import AssetService
    from erp.finance.deferred_tax_service import DeferredTaxService
    from erp.finance.forex_service import ForexTranslationService
    from erp.finance.intangible_service import IntangibleAssetService
    from erp.hr.retirement_service import RetirementService
    from erp.scm.valuation_service import ValuationService

    def _ok(label, res):
        print(f"  ✓ {label}: {res}")

    asvc = AssetService()
    # 감가상각(당월) + 손상(MRI) + 재평가(CT, fair_value_level 설정)
    n = await asvc.run_depreciation(session, period=YM)
    _ok("감가상각", f"{n}건")
    _ok("자산손상(MRI)", await asvc.impair(
        session, asset_id=refs["mri_id"], recoverable_amount=400_000_000, on_date=_month_end(YM)))
    _ok("자산재평가(CT)", await asvc.revalue(
        session, asset_id=refs["ct_id"], fair_value=560_000_000,
        on_date=_month_end(YM), fair_value_level="2"))
    _ok("무형상각(EMR)", await IntangibleAssetService().amortize_monthly(
        session, asset_id=refs["soft_id"], period=YM))

    _ok("대손충당금", await AllowanceService().post(session, yyyymm=YM))
    _ok("재고저가법", await ValuationService().nrv_post(session, yyyymm=YM))
    _ok("외화환산", await ForexTranslationService().post(
        session, account_id=refs["fx_acct_id"], fc_balance=100_000, rate=1_300, yyyymm=YM))

    if refs.get("payroll_count"):
        rs = RetirementService()
        _ok("퇴직충당적립", await rs.accrue_monthly(session, period=YM))
        _ok("퇴직DBO재측정", await rs.dbo_post(session, period=YM))
    else:
        print("  - 퇴직(충당·DBO): 급여 source 없음 — 생략")

    # 이연법인세는 마지막(1190·1159·2120 잔액을 일시적차이로 읽음)
    _ok("이연법인세", await DeferredTaxService().post(session, year=YEAR))


def _month_end(yyyymm: str) -> date:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1))


async def main() -> None:
    from erp.config import get_settings
    from erp.db import session_factory

    # prod 가드 — 결산엔진(감가/대손/이연법인세 등)이 실 GL 전체에 전기되어 실재무제표를 오염시킬 수
    # 있으므로, env=prod에서는 명시 플래그(--force-prod) 없으면 거부. (데모/파일럿 의도 시에만 강제)
    if get_settings().env == "prod" and "--force-prod" not in sys.argv:
        print("⚠ 거부 — env=prod입니다. 이 스크립트는 결산엔진을 실 GL에 전기(실재무 반영)합니다.\n"
              "  데모/파일럿 목적이 확실하면 `--force-prod` 플래그로 재실행하세요. (제거 SQL은 파일 말미)")
        return

    print(f"IFRS 데모 시드 시작 — 기간 {YM} (year {YEAR})")
    factory = session_factory()
    async with factory() as session:
        print("[1/2] 계정·설정·합성 source 마스터")
        async with session.begin():
            refs = await _seed_masters(session)
        print("[2/2] IFRS 결산조정 전기(멱등)")
        async with session.begin():
            await _run_engines(session, refs)
    print("\n완료. /finance-ifrs(정합 현황)·/finance-statements·/finance-periods 에서 측정값 시연 가능.")
    print("측정엔진 9종(대손·저가법·이연법인세·감가/손상/재평가·무형상각·외화환산·퇴직DBO) 비-0 값 생성.")


# ── 데모 데이터 제거(파일럿 정리 시) ─────────────────────────────
# 전표(결산조정):  DELETE FROM finance.voucher_lines WHERE voucher_id IN
#   (SELECT id FROM finance.vouchers WHERE source_module IN ('closing-ifrs','asset','intangible','ifrs-demo')
#     AND source_ref LIKE '%TEST%' OR source_ref IN ('ifrs-demo-fx-deposit'));  -- 환경별 확인 후 실행
# 마스터:  party/asset_no/code/name 의 'TEST%' 필터로 finance.receivables·fixed_assets·
#   intangible_assets·corporate_accounts·related_parties(+txns)·contingencies, scm.items('TEST-NRV%')
#   삭제. (정확한 제거는 데모 적재 후 source_ref 목록으로 한정 권장)

if __name__ == "__main__":
    asyncio.run(main())
