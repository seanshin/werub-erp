"""HIS 인박스 consumer — assets/hr 핸들러·미구현 FAILED·멱등 재처리."""
from datetime import date, timedelta
from sqlalchemy import select

from erp.finance.models import FixedAsset
from erp.hr.models import Employee
from erp.integrations.inbound_consumer import InboundConsumer
from erp.integrations.models import HisInboundEvent

consumer = InboundConsumer()


def _ev(domain, event_type, ref, payload):
    return HisInboundEvent(domain=domain, event_type=event_type, source_ref=ref,
                           payload=payload, status="NEW")


def _seed_voucher_rules(session):
    """검진권 전기 기본규칙(마이그 0074 시드와 동일) — sqlite 테스트는 시드 미적용이라 수동 주입."""
    from erp.finance.models import VoucherPostingRule
    defs = [
        ("purchase", "1110", "2140", "totalAmount,amount", False, False),
        ("sub_purchase", None, None, None, False, True),
        ("resale", None, None, None, False, True),
        ("redeem", "2140", "4110", "amountUsed,amount", False, False),
        ("topup", "1110", "4110", "amount", False, False),
        ("refund", "2140", "1110", "refundAmount,amount", False, False),
        ("commission", "5520", "2110", "commissionAmount", True, False),
        ("settlement", "2110", "1110", "netPayableKrw,netPayable", False, False),
    ]
    session.add_all([VoucherPostingRule(
        line_type=lt, debit_account=dr, credit_account=cr, amount_source=src,
        reverse_on_negative=rev, skip=sk) for lt, dr, cr, src, rev, sk in defs])


def _seed_corp(session):
    """통화별 기본 법인계좌 — 외화 자금 레그 라우팅(KRW→1110, USD→1115)."""
    from erp.finance.models import CorporateAccount
    session.add_all([
        CorporateAccount(alias="원화주거래", bank_name="신한", account_no="KRW-1",
                         currency="KRW", gl_account_code="1110", is_default=True, is_active=True),
        CorporateAccount(alias="외화USD", bank_name="신한", account_no="USD-1",
                         currency="USD", gl_account_code="1115", is_default=True, is_active=True)])


async def test_equipment_acquired_creates_asset(session):
    session.add(_ev("assets", "equipment.acquired", "EVT-A1", {
        "assetNumber": "EQ-CT-9", "name": "CT 스캐너", "purchasePrice": 850000000,
        "purchaseDate": "2026-01-01", "category": "진단장비"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1 and out["failed"] == 0
    a = await session.scalar(select(FixedAsset).where(FixedAsset.asset_no == "EQ-CT-9"))
    assert a and float(a.acquisition_cost) == 850000000
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == "EVT-A1"))
    assert ev.status == "APPLIED"


async def test_staff_created_upserts_employee(session):
    session.add(_ev("hr", "staff.created", "EVT-H1", {
        "employeeNumber": "DR-020", "name": "윤소화", "department": "내과",
        "position": "전문의", "hireDate": "2026-03-01", "status": "ACTIVE"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    e = await session.scalar(select(Employee).where(Employee.employee_no == "DR-020"))
    assert e and e.name == "윤소화" and e.position == "전문의" and e.department_id is not None


async def test_staff_resigned_sets_resigned_on(session):
    session.add(_ev("hr", "staff.created", "EVT-H3",
                    {"employeeNumber": "DR-099", "name": "퇴", "status": "ACTIVE"}))
    await session.flush()
    await consumer.consume(session)
    session.add(_ev("hr", "staff.resigned", "EVT-H4",
                    {"employeeNumber": "DR-099", "status": "RESIGNED", "resignDate": "2026-06-01"}))
    await session.flush()
    await consumer.consume(session)
    e = await session.scalar(select(Employee).where(Employee.employee_no == "DR-099"))
    assert e.resigned_on is not None


async def test_unimplemented_marked_failed(session):
    # 핸들러 없는 이벤트 → DEAD 가시화(영구실패, 무한 재시도 안 함 — P0-9)
    session.add(_ev("materials", "unknown.future_event", "EVT-M1", {"x": 1}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["failed"] == 1
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == "EVT-M1"))
    assert ev.status == "DEAD" and "미구현" in (ev.error or "")


async def test_cssd_po_approved_ack(session):
    session.add(_ev("procurement", "cssd.po.approved", "EVT-CSSD1", {
        "poNo": "PO-20260609-001", "supplierId": "sup-1", "totalAmount": 1250000,
        "approvedAt": "2026-06-09T00:00:00Z",
        "lines": [{"itemId": "i1", "itemName": "거즈(멸균)", "qty": 100, "unitPrice": 500}]}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == "EVT-CSSD1"))
    assert ev.status == "APPLIED"


async def test_voucher_vendor_synced_upserts_supplier(session):
    """vendor.synced(국내 딜러) → scm.Supplier upsert(dealerType·수수료율·활성)."""
    from erp.scm.models import Supplier
    session.add(_ev("finance", "vendor.synced", "voucher:dealer:D-1", {
        "vendorType": "VOUCHER_DEALER", "dealerId": "D-1", "name": "국내유치업 A",
        "dealerType": "DOMESTIC", "tier": "MASTER", "country": "KR", "currency": "KRW",
        "commissionRate": 12, "status": "ACTIVE", "email": "d1@hospital.example.org",
        "licenseNumber": "유치업-2026-001", "parentId": None}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    sup = await session.scalar(select(Supplier).where(Supplier.external_vendor_code == "D-1"))
    assert sup and sup.name == "국내유치업 A" and sup.dealer_type == "DOMESTIC"
    assert float(sup.commission_rate) == 12 and sup.is_active and sup.business_no is None
    assert sup.license_number == "유치업-2026-001"   # 법정 등록번호 보존(0071)


async def test_voucher_vendor_tax_bank_master(session):
    """B2-a — taxId·bankInfo: 민감정보 AES-GCM 암호화 저장(평문 컬럼 없음)·복호 라운드트립."""
    from erp.common import crypto
    from erp.scm.models import Supplier
    session.add(_ev("finance", "vendor.synced", "voucher:dealer:D-TB", {
        "dealerId": "D-TB", "name": "세무딜러", "dealerType": "OVERSEAS", "status": "ACTIVE",
        "taxId": "US-TIN-99", "bankInfo": {"account": "123-456", "bank": "Chase", "swift": "CHASUS33"}}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    sup = await session.scalar(select(Supplier).where(Supplier.external_vendor_code == "D-TB"))
    assert sup.bank_name == "Chase"                          # 비민감 평문
    assert sup.tax_id_enc is not None and isinstance(sup.tax_id_enc, bytes)
    assert crypto.decrypt_column(sup.tax_id_enc,
        aad=crypto.column_aad("scm.suppliers.tax_id", sup.id)) == "US-TIN-99"
    assert crypto.decrypt_column(sup.bank_account_enc,
        aad=crypto.column_aad("scm.suppliers.bank_account", sup.id)) == "123-456"
    assert crypto.decrypt_column(sup.bank_swift_enc,
        aad=crypto.column_aad("scm.suppliers.bank_swift", sup.id)) == "CHASUS33"


async def test_voucher_vendor_overseas_no_business_no(session):
    """해외 딜러는 business_no 없이 등록(마이그 0070 nullable) — currency 외화."""
    from erp.scm.models import Supplier
    session.add(_ev("finance", "vendor.synced", "voucher:dealer:D-2", {
        "vendorType": "VOUCHER_DEALER", "dealerId": "D-2", "name": "Overseas A",
        "dealerType": "OVERSEAS", "country": "US", "currency": "USD", "status": "ACTIVE"}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    sup = await session.scalar(select(Supplier).where(Supplier.external_vendor_code == "D-2"))
    assert sup.dealer_type == "OVERSEAS" and sup.currency == "USD" and sup.business_no is None


async def test_voucher_vendor_terminated_deactivates(session):
    """vendor.changed status=TERMINATED → 동일 dealerId 거래처 비활성(멱등 upsert)."""
    from erp.scm.models import Supplier
    session.add(_ev("finance", "vendor.synced", "voucher:dealer:D-3",
                    {"dealerId": "D-3", "name": "딜러 C", "dealerType": "DOMESTIC", "status": "ACTIVE"}))
    await session.flush()
    await consumer.consume(session)
    session.add(_ev("finance", "vendor.changed", "voucher:dealer:D-3:TERMINATED",
                    {"dealerId": "D-3", "name": "딜러 C", "dealerType": "DOMESTIC", "status": "TERMINATED"}))
    await session.flush()
    await consumer.consume(session)
    rows = list((await session.execute(
        select(Supplier).where(Supplier.external_vendor_code == "D-3"))).scalars())
    assert len(rows) == 1 and rows[0].is_active is False    # 신규 행 아님 — 동일 거래처 갱신


async def test_claim_reviewed_caps_reduced_by_submitted_when_claimed_absent(session):
    """전체검토 — 리뷰 페이로드에 claimedAmount 누락 시 제출 청구액으로 삭감 상한(1140 음수·삭감손실 과대 방지)."""
    from erp.finance.models import Voucher, VoucherLine
    session.add(_ev("finance", "claim.submitted", "claim:submit:CX1",
                    {"claimId": "CX1", "claimNumber": "CX1", "claimedAmount": 3000000,
                     "careMonth": "2026-07", "submittedAt": "2026-07-10"}))
    await session.flush()
    await consumer.consume(session)
    # 리뷰 — reducedAmount 500만(청구 초과)·claimedAmount 누락 → 제출 300만으로 상한
    session.add(_ev("finance", "claim.reviewed", "claim:review:CX1",
                    {"claimId": "CX1", "status": "REDUCED", "reducedAmount": 5000000}))
    await session.flush()
    await consumer.consume(session)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "claim:review:CX1"))
    loss = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    assert float(loss.amount) == 3000000   # 무상한이면 5,000,000(1140 −200만)이 됐을 것


async def test_voucher_purchase_unearned(session):
    """purchase(병원→딜러 선결제) → 선수금 Cr 2140 (DB-3 확정)."""
    from erp.finance.models import Voucher, VoucherLine
    _seed_voucher_rules(session)
    session.add(_ev("finance", "voucher.line_recorded", "voucher:purchase:A1", {
        "lineType": "purchase", "source": "voucher", "fromType": "HOSPITAL",
        "toDistributorId": "D1", "totalAmount": 2700000, "currency": "KRW", "paidAt": "2026-06-18"}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:purchase:A1"))
    cr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "CREDIT"))
    assert cr.account_code == "2140" and float(cr.amount) == 2700000


async def test_voucher_redeem_revenue(session):
    """redeem(검진 사용) → 선수금 해소·수익 인식(Dr 2140 / Cr 4110)."""
    from erp.finance.models import Voucher, VoucherLine
    _seed_voucher_rules(session)
    session.add(_ev("finance", "voucher.line_recorded", "voucher:redeem:R1", {
        "lineType": "redeem", "serial": "WGC1", "amountUsed": 150000}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:redeem:R1"))
    dr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    cr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "CREDIT"))
    assert dr.account_code == "2140" and cr.account_code == "4110"


async def test_voucher_commission_accrual_and_clawback(session):
    """commission 거래별 적립(Dr 5520/Cr 2110)·clawback(음수)=역분개(결정①)."""
    from erp.finance.models import Voucher, VoucherLine
    _seed_voucher_rules(session)
    session.add_all([
        _ev("finance", "voucher.line_recorded", "voucher:commission:C1",
            {"lineType": "commission", "commissionAmount": 90000, "distributorId": "D1"}),
        _ev("finance", "voucher.line_recorded", "voucher:commission:C2",
            {"lineType": "commission", "commissionAmount": -45000, "distributorId": "D1", "clawback": True})])
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 2
    v1 = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:commission:C1"))
    d1 = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v1.id, VoucherLine.side == "DEBIT"))
    assert d1.account_code == "5520" and float(d1.amount) == 90000
    v2 = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:commission:C2"))
    d2 = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v2.id, VoucherLine.side == "DEBIT"))
    assert d2.account_code == "2110" and float(d2.amount) == 45000     # clawback 역분개


async def test_voucher_resale_no_hospital_entry(session):
    """resale(딜러→환자)·sub_purchase(딜러간)는 병원 무전표(이중계상 방지)."""
    from erp.finance.models import Voucher
    _seed_voucher_rules(session)
    session.add(_ev("finance", "voucher.line_recorded", "voucher:resale:S1",
                    {"lineType": "resale", "serial": "WGC1", "salePrice": 300000}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    assert not await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:resale:S1"))


async def test_voucher_foreign_currency_deferred(session):
    """외화(USD) purchase → 환율(DB-2) 미정으로 DEAD 보존(재처리 가능)."""
    _seed_voucher_rules(session)
    session.add(_ev("finance", "voucher.line_recorded", "voucher:purchase:USD1",
                    {"lineType": "purchase", "fromType": "HOSPITAL", "totalAmount": 1000, "currency": "USD"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["failed"] == 1
    ev = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == "voucher:purchase:USD1"))
    assert ev.status == "DEAD" and "외화" in (ev.error or "")


async def test_voucher_settlement_ap_payment(session):
    """settlement(voucher.settled) 외화 → 정산 지급 netPayableKrw, 자금 레그는 외화계좌(1115)로 라우팅."""
    from erp.finance.models import Voucher, VoucherLine
    _seed_voucher_rules(session)
    _seed_corp(session)
    session.add(_ev("finance", "voucher.settled", "voucher:settlement:S9", {
        "lineType": "settlement", "distributorId": "D1", "dealerType": "OVERSEAS",
        "netPayable": 1000, "netPayableKrw": 1350000, "currency": "USD", "fxRate": 1350}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1     # 외화여도 netPayableKrw로 통과
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:settlement:S9"))
    dr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    cr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "CREDIT"))
    assert dr.account_code == "2110" and float(dr.amount) == 1350000   # netPayableKrw
    assert cr.account_code == "1115"     # 원화계좌(1110) 아닌 외화예금(1115)로 라우팅
    # DB-4 — 정산 부킹 시 지급 회신 아웃박스 enqueue(멱등)
    from erp.finance.models import DealerSettlementCallback
    cb = await session.scalar(select(DealerSettlementCallback).where(
        DealerSettlementCallback.event_id == "erp:settle-paid:S9"))
    assert cb and cb.status == "QUEUED" and float(cb.amount_paid_krw) == 1350000


async def test_settlement_withholding_recalc_3line(session):
    """원천징수 요율 설정 시 시점 재계산 → 3-line(Dr 2110/Cr 1110 net+Cr 2150 예수금)."""
    from datetime import date as _date
    from erp.finance.models import (
        DealerSettlementCallback, Voucher, VoucherLine, WithholdingRate)
    _seed_voucher_rules(session)
    session.add(WithholdingRate(dealer_type="DOMESTIC", rate_pct=3.3,
                effective_from=_date(2026, 1, 1), withholding_account="2150"))
    await session.flush()
    session.add(_ev("finance", "voucher.settled", "voucher:settlement:WH1", {
        "lineType": "settlement", "distributorId": "D1", "dealerType": "DOMESTIC",
        "netPayableKrw": 1000000, "currency": "KRW", "paidAt": "2026-06-18"}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:settlement:WH1"))
    by_acc = {ln.account_code: (ln.side, float(ln.amount)) for ln in
              (await session.execute(select(VoucherLine).where(
                  VoucherLine.voucher_id == v.id))).scalars()}
    assert by_acc["2110"] == ("DEBIT", 1000000)
    assert by_acc["2150"] == ("CREDIT", 33000)        # 3.3% 재계산
    assert by_acc["1110"] == ("CREDIT", 967000)        # net 지급
    cb = await session.scalar(select(DealerSettlementCallback).where(
        DealerSettlementCallback.settlement_id == "WH1"))
    assert float(cb.withholding_krw) == 33000


async def test_settlement_his_withholding_preferred(session):
    """HIS 동봉 withholdingKrw 우선(요율 미설정이어도 동봉값으로 3-line)."""
    from erp.finance.models import Voucher, VoucherLine
    _seed_voucher_rules(session)
    session.add(_ev("finance", "voucher.settled", "voucher:settlement:WH2", {
        "lineType": "settlement", "dealerType": "DOMESTIC", "netPayableKrw": 1000000,
        "withholdingKrw": 50000, "currency": "KRW"}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:settlement:WH2"))
    cr2150 = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.account_code == "2150"))
    assert float(cr2150.amount) == 50000               # HIS 동봉값(요율 무관)


# ─────────────────── Stage 2: 외화 라인레벨 fxRate 라우팅(외화계좌 1115) ───────────────────
async def test_voucher_foreign_purchase_routed_with_fxrate(session):
    """외화(USD) purchase + 라인 fxRate → KRW 환산 후 외화예금(1115)로 라우팅(Cr 선수금 2140)."""
    from erp.finance.models import Voucher, VoucherLine
    _seed_voucher_rules(session)
    _seed_corp(session)
    session.add(_ev("finance", "voucher.line_recorded", "voucher:purchase:USDFX", {
        "lineType": "purchase", "fromType": "HOSPITAL", "toDistributorId": "D1",
        "totalAmount": 2000, "currency": "USD", "fxRate": 1350, "paidAt": "2026-06-18"}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:purchase:USDFX"))
    dr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    cr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "CREDIT"))
    assert dr.account_code == "1115" and float(dr.amount) == 2700000   # 2000 USD × 1350, 외화계좌
    assert cr.account_code == "2140"                                   # 선수금(원화 미혼입)


async def test_voucher_foreign_no_fxrate_still_dead(session):
    """외화 라인 fxRate 미동봉 → 여전히 DEAD 보존(환율 확정 후 재처리)."""
    _seed_voucher_rules(session)
    _seed_corp(session)
    session.add(_ev("finance", "voucher.line_recorded", "voucher:purchase:USDNOFX", {
        "lineType": "purchase", "fromType": "HOSPITAL", "totalAmount": 1000, "currency": "USD"}))
    await session.flush()
    assert (await consumer.consume(session))["failed"] == 1
    ev = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == "voucher:purchase:USDNOFX"))
    assert ev.status == "DEAD" and "환율" in (ev.error or "")


async def test_voucher_foreign_no_corp_account_dead(session):
    """외화 법인계좌 미등록 → DEAD(원화 혼입 방지·설정 유도)."""
    _seed_voucher_rules(session)   # 법인계좌 미시드
    session.add(_ev("finance", "voucher.line_recorded", "voucher:purchase:USDNOACC", {
        "lineType": "purchase", "fromType": "HOSPITAL", "totalAmount": 1000,
        "currency": "USD", "fxRate": 1350}))
    await session.flush()
    assert (await consumer.consume(session))["failed"] == 1
    ev = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == "voucher:purchase:USDNOACC"))
    assert ev.status == "DEAD" and "법인계좌" in (ev.error or "")


async def test_voucher_foreign_commission_converted(session):
    """외화 commission + fxRate → KRW 환산 부킹(Dr 5520/Cr 2110, 비자금 레그라 라우팅 없음)."""
    from erp.finance.models import Voucher, VoucherLine
    _seed_voucher_rules(session)
    _seed_corp(session)
    session.add(_ev("finance", "voucher.line_recorded", "voucher:commission:USDC", {
        "lineType": "commission", "commissionAmount": 100, "currency": "USD",
        "fxRate": 1350, "distributorId": "D1"}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:commission:USDC"))
    dr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    assert dr.account_code == "5520" and float(dr.amount) == 135000    # 100 USD × 1350


async def test_settlement_no_rate_stays_2line(session):
    """요율 미설정·동봉 없음 → 무원천징수(2-line 유지, 2150 없음)."""
    from erp.finance.models import Voucher, VoucherLine
    _seed_voucher_rules(session)
    session.add(_ev("finance", "voucher.settled", "voucher:settlement:WH3", {
        "lineType": "settlement", "dealerType": "DOMESTIC", "netPayableKrw": 1000000, "currency": "KRW"}))
    await session.flush()
    await consumer.consume(session)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:settlement:WH3"))
    accs = [ln.account_code for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).scalars()]
    assert "2150" not in accs and set(accs) == {"2110", "1110"}


async def test_settlement_foreign_with_withholding_3line_routed(session):
    """외화 정산 + 원천징수 결합(무커버 경로) — netPayable(외화)×fxRate 환산 후 3-line.

    Dr 2110(미지급) / Cr 외화계좌 1115(net 지급, 통화별 라우팅) / Cr 예수금 2150(원천징수, 원화 유지).
    원천징수 예수금은 원화 계정이라 외화계좌로 라우팅되지 않음(원화·외화 혼입 방지 불변식).
    """
    from erp.finance.models import DealerSettlementCallback, Voucher, VoucherLine
    _seed_voucher_rules(session)
    _seed_corp(session)
    session.add(_ev("finance", "voucher.settled", "voucher:settlement:FXWH", {
        "lineType": "settlement", "settlementId": "FXWH", "distributorId": "D1",
        "dealerType": "DOMESTIC", "netPayable": 1000, "currency": "USD", "fxRate": 1350,
        "withholdingKrw": 50000, "paidAt": "2026-06-18"}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:settlement:FXWH"))
    by_acc = {ln.account_code: (ln.side, float(ln.amount)) for ln in
              (await session.execute(select(VoucherLine).where(
                  VoucherLine.voucher_id == v.id))).scalars()}
    assert by_acc["2110"] == ("DEBIT", 1350000)         # 1000 USD × 1350 환산
    assert by_acc["1115"] == ("CREDIT", 1300000)        # net(1,350,000 − 50,000) → 외화계좌 라우팅
    assert by_acc["2150"] == ("CREDIT", 50000)          # 원천징수 예수금 — 원화(외화계좌로 안 감)
    cb = await session.scalar(select(DealerSettlementCallback).where(
        DealerSettlementCallback.settlement_id == "FXWH"))
    assert float(cb.amount_paid_krw) == 1350000 and float(cb.withholding_krw) == 50000


async def test_settlement_event_type_fallback_no_linetype(session):
    """검토보완 — voucher.settled에 lineType 미동봉이어도 event_type으로 settlement 규칙 적용(DEAD 방지)."""
    from erp.finance.models import Voucher
    _seed_voucher_rules(session)
    session.add(_ev("finance", "voucher.settled", "voucher:settlement:NOLT",
                    {"distributorId": "D1", "netPayableKrw": 500000, "currency": "KRW"}))  # lineType 없음
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    assert await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:settlement:NOLT"))


async def test_posting_rule_no_fuzzy_fallback(session):
    """검토보완 — 딜러유형 구체·NULL 규칙 모두 없으면 미설정(DEAD), 타 유형 규칙 fuzzy 적용 안 함."""
    from erp.finance.models import Voucher, VoucherPostingRule
    session.add(VoucherPostingRule(line_type="commission", dealer_type="DOMESTIC",
                debit_account="5520", credit_account="2110", amount_source="commissionAmount"))
    await session.flush()
    session.add(_ev("finance", "voucher.line_recorded", "voucher:commission:OVX",
                    {"lineType": "commission", "dealerType": "OVERSEAS", "commissionAmount": 10000}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["failed"] == 1                          # DOMESTIC 규칙을 OVERSEAS에 적용 안 함
    ev = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == "voucher:commission:OVX"))
    assert ev.status == "DEAD" and "규칙" in (ev.error or "")
    assert not await session.scalar(select(Voucher).where(
        Voucher.source_ref == "voucher:commission:OVX"))


async def test_settlement_withholding_capped_at_amount(session):
    """검토보완 — 원천징수액이 지급액 초과해도 net 음수 라인 없음(wh<=amt 가드)."""
    from erp.finance.models import Voucher, VoucherLine
    _seed_voucher_rules(session)
    session.add(_ev("finance", "voucher.settled", "voucher:settlement:WHCAP", {
        "lineType": "settlement", "dealerType": "DOMESTIC", "netPayableKrw": 100000,
        "withholdingKrw": 999999, "currency": "KRW"}))   # 비정상 과대 원천징수
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:settlement:WHCAP"))
    for ln in (await session.execute(select(VoucherLine).where(
            VoucherLine.voucher_id == v.id))).scalars():
        assert float(ln.amount) >= 0                   # 음수 라인 없음


async def test_voucher_rule_override(session):
    """관리자 규칙 변경 반영 — commission 방향을 수익(Dr 1130/Cr 4xxx)으로 바꾸면 그대로 전기."""
    from erp.finance.models import Voucher, VoucherLine, VoucherPostingRule
    session.add(VoucherPostingRule(line_type="commission", debit_account="1130",
                credit_account="4190", amount_source="commissionAmount", reverse_on_negative=True))
    await session.flush()
    session.add(_ev("finance", "voucher.line_recorded", "voucher:commission:OV1",
                    {"lineType": "commission", "commissionAmount": 50000}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "voucher:commission:OV1"))
    dr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    cr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "CREDIT"))
    assert dr.account_code == "1130" and cr.account_code == "4190"     # 관리자 설정대로


async def test_voucher_missing_rule_dead(session):
    """전기 규칙 미설정 lineType → DEAD(전표 안 만듦, 가시화)."""
    session.add(_ev("finance", "voucher.line_recorded", "voucher:redeem:NORULE",
                    {"lineType": "redeem", "amountUsed": 1000}))     # 규칙 시드 안 함
    await session.flush()
    out = await consumer.consume(session)
    assert out["failed"] == 1
    ev = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == "voucher:redeem:NORULE"))
    assert ev.status == "DEAD" and "규칙" in (ev.error or "")


async def test_staff_schedule_aggregates_attendance(session):
    from erp.hr.models import AttendanceSummary, Employee
    session.add(Employee(employee_no="DR-301", name="schedtest", external_user_id="clinic-301", hired_on=date(2020, 1, 1)))
    await session.flush()
    session.add(_ev("hr", "staff.schedule_changed", "EVT-SCH1", {
        "employeeNumber": "DR-301", "schedules": [
            {"date": "2026-06-01", "shiftType": "DAY", "startTime": "09:00", "endTime": "18:00"},
            {"date": "2026-06-02", "shiftType": "NIGHT", "startTime": "22:00", "endTime": "07:00"}]}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    s = await session.scalar(select(AttendanceSummary).where(AttendanceSummary.yyyymm == "2026-06"))
    assert s.work_days == 2 and s.night_shift_count == 1 and s.total_work_minutes == 540 + 540


async def test_staff_qualification_upserts_license(session):
    from erp.hr.models import Employee, License
    session.add(Employee(employee_no="DR-302", name="qualtest", external_user_id="clinic-302", hired_on=date(2020, 1, 1)))
    await session.flush()
    session.add(_ev("hr", "staff.qualification_changed", "EVT-Q1", {
        "userId": "clinic-302", "credentialId": "MD-12345", "type": "의사",
        "name": "의사면허", "expiresAt": "2030-12-31", "status": "ACTIVE"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    lic = await session.scalar(select(License).where(License.license_no == "MD-12345"))
    assert lic.license_type == "의사" and lic.expires_on.isoformat() == "2030-12-31"


async def test_applied_not_reprocessed(session):
    session.add(_ev("hr", "staff.created", "EVT-H2",
                    {"employeeNumber": "DR-021", "name": "a", "status": "ACTIVE"}))
    await session.flush()
    await consumer.consume(session)
    out2 = await consumer.consume(session)
    assert out2["processed"] == 0       # APPLIED는 NEW 아님 — 재처리 안 함


async def test_billing_settled_creates_revenue_voucher(session):
    from erp.finance.models import Voucher, VoucherLine
    from sqlalchemy import select, func
    session.add(_ev("finance", "billing.settled", "EVT-BS1", {
        "date": "2026-06-09", "summary": {"byPaymentMethod": [
            {"paymentMethod": "CARD", "_sum": {"paidAmount": 1_000_000}},
            {"paymentMethod": "CASH", "_sum": {"paidAmount": 300_000}},
            {"paymentMethod": "TRANSFER", "_sum": {"paidAmount": 200_000}}]}}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "settle:2026-06-09"))
    assert v is not None
    # 차변 = 현금500k + 카드미수1M, 대변 = 수익1.5M (균형)
    debit = await session.scalar(select(func.sum(VoucherLine.amount)).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    assert float(debit) == 1_500_000


async def test_billing_settled_idempotent(session):
    payload = {"date": "2026-06-10", "summary": {"byPaymentMethod": [
        {"paymentMethod": "CASH", "_sum": {"paidAmount": 500_000}}]}}
    session.add(_ev("finance", "billing.settled", "EVT-BS2", payload))
    await session.flush()
    await consumer.consume(session)
    # 같은 일자 재수신(다른 event) → 전표 멱등(중복 생성 안 함)
    session.add(_ev("finance", "billing.settled", "EVT-BS2-dup", payload))
    await session.flush()
    await consumer.consume(session)
    from erp.finance.models import Voucher
    from sqlalchemy import select, func
    cnt = await session.scalar(select(func.count()).select_from(Voucher).where(
        Voucher.source_ref == "settle:2026-06-10"))
    assert cnt == 1


async def test_billing_adjusted_reverses(session):
    from erp.finance.models import Voucher
    from sqlalchemy import select
    session.add(_ev("finance", "billing.adjusted", "EVT-BA1", {
        "billingId": "B-99", "type": "REFUND", "amount": 50_000, "at": "2026-06-11T10:00:00"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref.like("adjust:B-99:REFUND:%")))
    assert v is not None


async def test_equipment_status_disposed(session):
    from sqlalchemy import select
    from erp.finance.models import FixedAsset
    session.add(_ev("assets", "equipment.acquired", "EVT-EQ-S", {
        "assetNumber": "EQ-S1", "name": "장비", "purchasePrice": 1_000_000,
        "purchaseDate": "2026-01-01"}))
    await session.flush()
    await consumer.consume(session)
    session.add(_ev("assets", "equipment.status_changed", "EVT-EQ-S2", {
        "assetNumber": "EQ-S1", "status": "DISPOSED"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    a = await session.scalar(select(FixedAsset).where(FixedAsset.asset_no == "EQ-S1"))
    assert a.status == "DISPOSED"


async def test_equipment_maintenance_voucher(session):
    from sqlalchemy import select
    from erp.finance.models import Voucher
    session.add(_ev("assets", "equipment.maintenance", "EVT-EQ-M", {
        "assetNumber": "EQ-M1", "maintenanceType": "정기점검", "cost": 300_000,
        "vendor": "GE", "performedAt": "2026-06-05"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    v = await session.scalar(select(Voucher).where(
        Voucher.source_ref == "maint:EQ-M1:2026-06-05"))
    assert v is not None


async def test_materials_stock_mirror(session):
    from sqlalchemy import select
    from erp.integrations.models import HisStockMirror
    session.add(_ev("materials", "stock.changed", "EVT-MAT1", {
        "code": "INV-001", "erpItemCode": "ERP-001", "name": "주사기 3mL",
        "type": "OUT", "qty": -50, "balanceAfter": 5050, "ref": "PO-1"}))
    session.add(_ev("materials", "inventory.changed", "EVT-MAT2", {
        "code": "INV-002", "name": "거즈", "type": "IN", "qty": 200}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 2
    rows = list((await session.execute(select(HisStockMirror).order_by(HisStockMirror.id))).scalars())
    assert len(rows) == 2
    assert rows[0].item_code == "ERP-001" and rows[0].source == "CSSD" and float(rows[0].qty) == -50
    assert rows[1].item_code == "INV-002" and rows[1].source == "INVENTORY"  # 미매핑은 HIS code


async def test_claim_submitted_ar_voucher(session):
    from sqlalchemy import select
    from erp.finance.models import Voucher
    session.add(_ev("finance", "claim.submitted", "EVT-CS1", {
        "claimId": "C-1", "claimNumber": "CLM-001", "claimedAmount": 120000, "status": "SUBMITTED"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    assert await session.scalar(select(Voucher).where(Voucher.source_ref == "claim:submit:C-1"))


async def test_claim_reviewed_loss_voucher(session):
    from sqlalchemy import select
    from erp.finance.models import Voucher
    session.add(_ev("finance", "claim.reviewed", "EVT-CR1", {
        "claimId": "C-2", "claimNumber": "CLM-002", "status": "PARTIALLY_APPROVED",
        "approvedAmount": 110000, "reducedAmount": 10000}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    assert await session.scalar(select(Voucher).where(Voucher.source_ref == "claim:review:C-2"))
    # 삭감 0이면 전표 없음(스킵)
    session.add(_ev("finance", "claim.reviewed", "EVT-CR2", {
        "claimId": "C-3", "status": "APPROVED", "approvedAmount": 100000, "reducedAmount": 0}))
    await session.flush()
    await consumer.consume(session)
    assert not await session.scalar(select(Voucher).where(Voucher.source_ref == "claim:review:C-3"))


async def test_pharmacy_dispensed_mirror(session):
    from sqlalchemy import select
    from erp.integrations.models import HisStockMirror
    session.add(_ev("materials", "pharmacy.dispensed", "EVT-PH1", {
        "dispensingId": "D-1", "items": [
            {"medicationCode": "MED-1", "name": "타이레놀500mg", "dispensedQty": 30},
            {"medicationCode": "MED-2", "name": "아목시실린", "dispensedQty": 14}]}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    rows = list((await session.execute(select(HisStockMirror).where(
        HisStockMirror.source == "PHARMACY"))).scalars())
    assert len(rows) == 2 and float(rows[0].qty) == -30 and rows[0].movement_type == "DISPENSE"


async def test_unhandled_event_goes_dead_not_stuck(session):
    """P0-9 — 핸들러 미구현 이벤트는 DEAD(영구실패 분리), 무한 재시도 안 함."""
    session.add(_ev("nope", "x.unknown", "EVT-DEAD", {}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["failed"] == 1
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == "EVT-DEAD"))
    assert ev.status == "DEAD"
    # 재소비 시 DEAD는 선택 안 됨(NEW/도래FAILED만)
    assert (await consumer.consume(session))["processed"] == 0


async def test_transient_failure_retries_then_dead(session):
    """P0-9 — 일시실패는 FAILED+next_retry_at(재시도), 한도 초과 시 DEAD."""
    from erp.integrations import inbound_consumer as ic

    async def _boom(session, p, et, ctx):
        raise ValueError("일시 실패(환자 미생성 등)")
    ic.HANDLERS[("test", "boom")] = _boom
    try:
        session.add(_ev("test", "boom", "EVT-RETRY", {}))
        await session.flush()
        out = await consumer.consume(session)        # 1회차 — FAILED, next_retry_at 설정
        ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == "EVT-RETRY"))
        assert out["failed"] == 1 and ev.status == "FAILED" and ev.retry_count == 1
        assert ev.next_retry_at is not None          # 백오프 — 즉시 재소비 안 됨
        assert (await consumer.consume(session))["processed"] == 0
    finally:
        ic.HANDLERS.pop(("test", "boom"), None)


# ── 이벤트 기반 표적 사전심사 글루 (PR #226 실시간 경로) ───────────────
# consume()이 billing.line_recorded 소비 후 (chart_no, care_month) 표적을
# 도출해야 worker가 표적별 사전심사를 자동 재평가한다. 이 수집부가 자동심사의 두뇌.

def _billing_ev(ref, chart_no, treated_on, line_id=None):
    return _ev("finance", "billing.line_recorded", ref, {
        "lineId": line_id or ref, "chartNo": chart_no, "code": "AA100",
        "treatedOn": treated_on})


async def test_billing_line_emits_prereview_target(session):
    """billing.line_recorded(treatedOn 有) 소비 → (chartNo, 'YYYY-MM') 표적 1건 도출."""
    session.add(_billing_ev("EVT-PR1", "C-PR1", "2026-06-11"))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1 and out["failed"] == 0
    assert out["prereview_targets"] == [("C-PR1", "2026-06")]


async def test_billing_line_without_treated_on_no_target(session):
    """treatedOn 없으면(_prereview 미방출) 라인은 APPLIED지만 표적 없음 —
    진료일 미상은 월귀속 불가라 자동 재평가 대상에서 제외."""
    session.add(_ev("finance", "billing.line_recorded", "EVT-PR-NODATE",
                    {"lineId": "L-ND", "chartNo": "C-ND", "code": "AA100"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    assert out["prereview_targets"] == []


async def test_prereview_targets_deduped_and_sorted(session):
    """동일 차트×월 다건은 1건으로 합치고(set), 다른 차트/월은 별개 표적으로 정렬 반환 —
    표적 sync 중복 호출 방지(멱등 부담 경감)·결정적 순서."""
    session.add_all([
        _billing_ev("EVT-D1", "C-A", "2026-06-01", line_id="L-A1"),
        _billing_ev("EVT-D2", "C-A", "2026-06-20", line_id="L-A2"),  # 같은 C-A·6월 → 중복
        _billing_ev("EVT-D3", "C-A", "2026-05-30", line_id="L-A3"),  # 같은 C-A·5월 → 별개
        _billing_ev("EVT-D4", "C-B", "2026-06-15", line_id="L-B1"),  # 다른 차트 → 별개
    ])
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 4
    assert out["prereview_targets"] == [
        ("C-A", "2026-05"), ("C-A", "2026-06"), ("C-B", "2026-06")]


async def test_cancelled_billing_line_still_targets_prereview(session):
    """취소(cancelled) 라인도 표적 도출 — 취소분 반영 위해 그 차트×월 재평가 필요
    (룰은 차트의 월 전체를 재평가하므로 취소 후 적발 해제도 반영)."""
    session.add(_ev("finance", "billing.line_recorded", "EVT-PR-CANCEL",
                    {"lineId": "L-CX", "chartNo": "C-CX", "code": "AA100",
                     "treatedOn": "2026-06-09", "cancelled": True}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    assert out["prereview_targets"] == [("C-CX", "2026-06")]


# ── 품질 심화(2026-07-04): 자금 차대균형 불변식·무커버 핸들러·DEAD 소진 ──────────
async def _voucher_balanced(session, source_ref) -> bool:
    """전표 차대균형 불변식 — sum(DEBIT)==sum(CREDIT). 자금 핸들러 회귀 가드."""
    from sqlalchemy import func, select

    from erp.finance.models import Voucher, VoucherLine
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == source_ref))
    assert v is not None, f"전표 없음: {source_ref}"
    dr = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    cr = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "CREDIT"))
    return float(dr) == float(cr)


async def test_billing_settled_balances_with_decimal_amounts(session):
    """라운딩 회귀 가드 — round(cash)+round(card) != round(cash+card)로 차대 불균형→전표 유실.

    cash=100.5·card=200.5(뱅커스 라운딩: 100·200, 합 300) vs round(301.0)=301. 수정 전엔
    대변 301·차변 300으로 ValidationError→FAILED. 수정 후 대변=차변합=300으로 균형·APPLIED."""
    session.add(_ev("finance", "billing.settled", "EVT-BS-DEC", {
        "date": "2026-06-11", "summary": {"byPaymentMethod": [
            {"paymentMethod": "CASH", "_sum": {"paidAmount": 100.5}},
            {"paymentMethod": "CARD", "_sum": {"paidAmount": 200.5}}]}}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1                       # 수정 전엔 failed(불균형)였음
    assert await _voucher_balanced(session, "settle:2026-06-11")


async def test_staff_attendance_clock_in_out_and_midnight(session):
    """무커버 핸들러 — CLOCK_IN/OUT 일자행 upsert + 자정 넘김 페어링(야간 교대)."""
    from sqlalchemy import select

    from erp.hr.models import GroupwareAttendanceStaging as GAS
    # 6/20 출근 09:00 · 익일 6/21 02:00 퇴근(자정 넘김) → 6/20 행에 페어링
    session.add(_ev("hr", "staff.attendance_recorded", "att:E1:0900:IN",
                    {"employeeNumber": "E1", "at": "2026-06-20T09:00:00", "type": "IN"}))
    session.add(_ev("hr", "staff.attendance_recorded", "att:E1:0200:OUT",
                    {"employeeNumber": "E1", "at": "2026-06-21T02:00:00", "type": "OUT"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 2
    rows = (await session.execute(select(GAS).where(GAS.employee_no == "E1"))).scalars().all()
    assert len(rows) == 1                            # 자정 넘김 OUT이 전일 IN 행에 페어링(새 행 없음)
    r = rows[0]
    assert r.work_on.isoformat() == "2026-06-20"
    # PG 레인은 진짜 UTC aware로 되읽힘 — raw .hour 단언은 -9h 왜곡(KST 환산 후 판정·v1.257.2)
    from erp.common.kst import as_kst_wallclock
    ci, co = as_kst_wallclock(r.clock_in), as_kst_wallclock(r.clock_out)
    assert ci.hour == 9 and co.day == 21 and co.hour == 2


async def test_staff_attendance_dup_punch_merge_pg_aware(session):
    """중복 출근 min() 병합 회귀(v1.257.2) — 기존 KST 09:00 행 + 신규 08:50 → 08:50 채택.

    PG 레인(TEST_DATABASE_URL)에선 기존 값이 진짜 UTC aware(=00:00Z)로 되읽히는데, 라벨-strip
    (-9h 왜곡)이면 00:00으로 잡혀 min()이 기존 왜곡값을 저장(오염)했다 — as_kst_wallclock 검증.
    sqlite 레인은 naive 왕복이라 병합 로직 자체만 검증된다."""
    from datetime import date as d
    from datetime import datetime as dt

    from sqlalchemy import select

    from erp.hr.models import GroupwareAttendanceStaging as GAS
    session.add(GAS(source_ref="his:E2:2026-06-20", external_user_id="his:E2",
                    employee_no="E2", work_on=d(2026, 6, 20), status="NEW",
                    clock_in=dt(2026, 6, 20, 9, 0)))   # naive KST(실 쓰기 경로와 동형)
    session.add(_ev("hr", "staff.attendance_recorded", "att:E2:0850:IN",
                    {"employeeNumber": "E2", "at": "2026-06-20T08:50:00", "type": "IN"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    from erp.common.kst import as_kst_wallclock
    r = await session.scalar(select(GAS).where(GAS.employee_no == "E2"))
    assert as_kst_wallclock(r.clock_in) == dt(2026, 6, 20, 8, 50)   # 08:50 < 09:00 → 신규 채택


async def test_staff_attendance_bad_payload_dead(session):
    """type 누락 → ValueError(일시실패 취급·핸들러 계약). FAILED·retry 1."""
    session.add(_ev("hr", "staff.attendance_recorded", "att:E9:bad",
                    {"employeeNumber": "E9", "at": "2026-06-20T09:00:00"}))  # type 없음
    await session.flush()
    out = await consumer.consume(session)
    assert out["failed"] == 1


async def test_transient_failure_reaches_dead_by_exhaustion(session):
    """MAX_RETRY(5) 소진 → DEAD 전이(기존 테스트는 1회차만 검증) — 경계 고정."""
    from datetime import datetime, timezone

    from sqlalchemy import select, update

    from erp.integrations import inbound_consumer as ic

    async def _boom(session, p, et, ctx):
        raise ValueError("일시 실패 반복")
    ic.HANDLERS[("test", "boomdead")] = _boom
    try:
        session.add(_ev("test", "boomdead", "EVT-EXH", {}))
        await session.flush()
        for _ in range(ic.InboundConsumer.MAX_RETRY):
            await consumer.consume(session)
            # next_retry_at을 과거로 밀어 즉시 재소비 대상화
            await session.execute(update(HisInboundEvent)
                                  .where(HisInboundEvent.source_ref == "EVT-EXH")
                                  .values(next_retry_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
        ev = await session.scalar(select(HisInboundEvent).where(
            HisInboundEvent.source_ref == "EVT-EXH"))
        assert ev.status == "DEAD" and ev.retry_count >= ic.InboundConsumer.MAX_RETRY
    finally:
        ic.HANDLERS.pop(("test", "boomdead"), None)


async def test_closed_period_event_dead_immediately(session):
    """마감기간 이벤트 → ValidationError 영구실패로 즉시 DEAD(재시도 없음).

    기존엔 일시실패로 5회(75분) 재시도 후 DEAD → 마감월 지각 이벤트가 조용히 매몰.
    이제 retry_count=0으로 즉시 DEAD·사유 가시화(역분개/개방월 처리 유도)."""
    from sqlalchemy import select

    from erp.finance.models import FiscalPeriod
    # 2026-05 마감
    session.add(FiscalPeriod(yyyymm="2026-05", fiscal_year=2026, status="CLOSED"))
    session.add(_ev("finance", "billing.settled", "EVT-CLOSED", {
        "date": "2026-05-15", "summary": {"byPaymentMethod": [
            {"paymentMethod": "CASH", "_sum": {"paidAmount": 500_000}}]}}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["failed"] == 1
    ev = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == "EVT-CLOSED"))
    assert ev.status == "DEAD" and ev.retry_count == 0   # 즉시 DEAD(재시도 안 함)
    assert "마감" in (ev.error or "")


async def test_transient_valueerror_still_retries(session):
    """대조군 — ValueError(환자/직원 미상)는 여전히 일시실패(FAILED·재시도). 회귀 방지."""
    from sqlalchemy import select

    from erp.integrations import inbound_consumer as ic

    async def _boom(session, p, et, ctx):
        raise ValueError("환자 미상(동기화 대기)")
    ic.HANDLERS[("test", "transient")] = _boom
    try:
        session.add(_ev("test", "transient", "EVT-TRANSIENT", {}))
        await session.flush()
        await consumer.consume(session)
        ev = await session.scalar(select(HisInboundEvent).where(
            HisInboundEvent.source_ref == "EVT-TRANSIENT"))
        assert ev.status == "FAILED" and ev.retry_count == 1   # 일시실패 유지
    finally:
        ic.HANDLERS.pop(("test", "transient"), None)


async def test_sign_completed_activates_approved_contract(session):
    """무커버 핸들러 — 서명완료 미러 + APPROVED→ACTIVE 자동 발효 트리거(위수탁)."""
    from datetime import date

    from erp.scm.models import OutsourcingContract
    c = OutsourcingContract(vendor_name="급식(주)", category="MEAL", title="급식 위탁",
                            start_on=date(2026, 1, 1), end_on=date(2027, 1, 1), status="APPROVED")
    session.add(c)
    await session.flush()
    session.add(_ev("sign", "sign.completed", "sign:OC:1", {
        "documentType": "ERP_OUTSOURCING_CONTRACT", "erpContractId": c.id,
        "status": "COMPLETED", "signatureRequestId": "req-1",
        "completedAt": "2026-06-20T10:00:00", "certSerial": "CERT-9"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    await session.refresh(c)
    assert c.sign_status == "COMPLETED" and c.sign_cert_serial == "CERT-9"
    assert c.status == "ACTIVE"                       # 서명완료→자동 발효(발효일 도래)


async def test_sign_completed_holds_when_before_effective(session):
    """발효일 미도래면 서명완료 미러만·발효 보류(status APPROVED 유지·에러 없음)."""
    from datetime import date, timedelta

    from erp.scm.models import OutsourcingContract
    future = date.today() + timedelta(days=30)
    c = OutsourcingContract(vendor_name="세탁(주)", category="LAUNDRY", title="세탁 위탁",
                            start_on=future, end_on=future + timedelta(days=365), status="APPROVED")
    session.add(c)
    await session.flush()
    session.add(_ev("sign", "sign.completed", "sign:OC:2", {
        "documentType": "ERP_OUTSOURCING_CONTRACT", "erpContractId": c.id, "status": "COMPLETED"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1                        # 보류는 에러 아님(로그 가시화)
    await session.refresh(c)
    assert c.sign_status == "COMPLETED" and c.status == "APPROVED"   # 발효일 전 → 보류


async def test_sign_completed_noop_for_non_erp_document(session):
    """CONSENT 등 비-ERP 봉투(HIS 환자도메인) → 무시(APPLIED·부작용 없음)."""
    session.add(_ev("sign", "sign.completed", "sign:CONSENT:1",
                    {"documentType": "CONSENT", "status": "COMPLETED"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1                        # no-op이지만 APPLIED(에러 아님)


async def test_cost_period_closed_ack(session):
    """원가 기간마감 통지(카탈로그 v1 신규) — 정보성 확인·APPLIED(미구현 DEAD 아님)·비가역 처리 없음."""
    session.add(_ev("finance", "cost.period_closed", "EVT-COST1", {
        "period": "2026-05", "closedAt": "2026-06-05T00:00:00Z"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == "EVT-COST1"))
    assert ev.status == "APPLIED" and ev.error is None      # 미구현 DEAD 아님(정보성 처리)


async def test_claim_reviewed_derives_reduction_from_approved(session):
    """강화 필드 — reducedAmount 미제공 시 claimedAmount−approvedAmount로 삭감 보정(정합·카탈로그 v1)."""
    from sqlalchemy import select

    from erp.finance.models import Voucher, VoucherLine
    session.add(_ev("finance", "claim.reviewed", "EVT-CR3", {
        "claimId": "C-9", "claimNumber": "CLM-009", "status": "PARTIALLY_APPROVED",
        "claimedAmount": 100000, "approvedAmount": 85000}))    # reducedAmount 없음 → 15000 보정
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "claim:review:C-9"))
    assert v is not None
    loss = await session.scalar(select(VoucherLine.amount).where(
        VoucherLine.voucher_id == v.id, VoucherLine.account_code == "5910"))
    assert round(float(loss)) == 15000       # 청구 100k − 확정 85k = 삭감 15k


# ── sign.completed (e-sign #6 미러·발효 트리거·완료본 회수 outbox) ──────────
def _oc(session, **kw):
    """APPROVED 위수탁 계약 시드(발효 트리거 대상)."""
    from erp.scm.models import OutsourcingContract
    defaults = dict(vendor_name="한빛급식", category="MEAL", contract_type="OUTSOURCING",
                    billing_type="MONTHLY", title="급식 위탁", monthly_amount=3_000_000,
                    start_on=date(2026, 1, 1), end_on=date(2026, 12, 31), status="APPROVED")
    defaults.update(kw)
    c = OutsourcingContract(**defaults)
    session.add(c)
    return c


async def test_sign_completed_outsourcing_activates(session):
    """위수탁 e-sign 완료 미러 → 서명필드 반영 + APPROVED→ACTIVE 발효(발효일 도래)."""
    from erp.scm.models import OutsourcingContract
    c = _oc(session, effective_on=date(2026, 1, 1))
    await session.flush()
    session.add(_ev("sign", "sign.completed", "EVT-SIGN1", {
        "documentType": "ERP_OUTSOURCING_CONTRACT", "erpContractId": c.id,
        "signatureRequestId": "ENV-77", "status": "COMPLETED",
        "completedAt": "2026-02-01T09:00:00+00:00", "certSerial": "CERT-9",
        "assuranceLevel": "AL2", "auditCertUri": "https://sign.his/audit/ENV-77"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1 and out["failed"] == 0
    row = await session.get(OutsourcingContract, c.id)
    assert row.sign_status == "COMPLETED" and row.sign_envelope_id == "ENV-77"
    assert row.sign_cert_serial == "CERT-9" and row.sign_doc_ref == "https://sign.his/audit/ENV-77"
    assert row.status == "ACTIVE"                     # 서명완료 → 발효 트리거


async def test_sign_completed_activation_held_before_effective(session):
    """발효일 미도래 — 서명은 미러하되 발효는 보류(ValidationError 흡수·APPLIED, FAILED 아님)."""
    from erp.scm.models import OutsourcingContract
    # 미래 발효일 — 오늘 기준(고정일을 쓰면 달력이 지나는 순간 '미도래' 전제가 깨진다)
    c = _oc(session, effective_on=date.today() + timedelta(days=90))
    await session.flush()
    session.add(_ev("sign", "sign.completed", "EVT-SIGN2", {
        "documentType": "ERP_OUTSOURCING_CONTRACT", "erpContractId": c.id,
        "signatureRequestId": "ENV-88", "status": "COMPLETED",
        "completedAt": "2026-02-01T09:00:00+00:00"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1 and out["failed"] == 0   # 발효 보류는 실패 아님
    row = await session.get(OutsourcingContract, c.id)
    assert row.sign_status == "COMPLETED" and row.sign_envelope_id == "ENV-88"
    assert row.status == "APPROVED"                     # 발효일 전 — ACTIVE 승격 안 됨


async def test_sign_completed_non_erp_document_noop(session):
    """ERP 계약 외 봉투(CONSENT 등) — ERP 무관 no-op APPLIED(DEAD 아님)."""
    session.add(_ev("sign", "sign.completed", "EVT-SIGN3", {
        "documentType": "CONSENT", "signatureRequestId": "ENV-99", "status": "COMPLETED"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1 and out["failed"] == 0
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == "EVT-SIGN3"))
    assert ev.status == "APPLIED" and ev.error is None


async def test_sign_completed_medical_enqueues_esign_fetch(session):
    """의료진 계약 완료본(doc.url) — 인박스 트랜잭션에 GET 묶지 않고 outbox enqueue(별도 잡 회수).

    단기 HMAC 토큰 url은 sign_doc_ref에 저장 금지(auditCertUri만 추적ref).
    """
    from erp.hr.models import Employee, MedicalStaffContract
    from erp.integrations.models import EsignFetchOutbox
    emp = Employee(employee_no="DR-777", name="김서명", source="ERP", hired_on=date(2026, 1, 1))
    session.add(emp)
    await session.flush()
    c = MedicalStaffContract(employee_id=emp.id, valid_from=date(2026, 1, 1),
                             effective_on=date(2026, 1, 1), base_pay=8_000_000, status="APPROVED")
    session.add(c)
    await session.flush()
    session.add(_ev("sign", "sign.completed", "EVT-SIGN4", {
        "documentType": "ERP_MEDICAL_CONTRACT", "erpContractId": c.id,
        "signatureRequestId": "ENV-100", "status": "COMPLETED",
        "auditCertUri": "https://sign.his/audit/ENV-100",
        "document": {"url": "https://sign.his/doc/ENV-100?token=short24h", "issueNo": "ISS-5"}}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1 and out["failed"] == 0
    row = await session.get(MedicalStaffContract, c.id)
    assert row.sign_status == "COMPLETED" and row.status == "ACTIVE"
    assert row.sign_doc_ref == "https://sign.his/audit/ENV-100"   # 단기토큰 url 저장 안 함
    ob = await session.scalar(select(EsignFetchOutbox).where(
        EsignFetchOutbox.entity_type == "medical_contract",
        EsignFetchOutbox.contract_id == c.id))
    assert ob is not None and ob.status == "PENDING"
    assert ob.doc_url == "https://sign.his/doc/ENV-100?token=short24h" and ob.issue_no == "ISS-5"


# ── claim.reviewed 삭감 폴백 안전(재검토 보완) ──────────────────────────
async def test_claim_reviewed_no_phantom_loss_when_approved_missing(session):
    """approvedAmount 누락+승인계열 status → 전액 삭감손실 오계상 금지(판정 보류)."""
    from erp.finance.models import Voucher
    session.add(_ev("finance", "claim.reviewed", "EVT-CRPH", {
        "claimId": "C-PH", "status": "PARTIALLY_APPROVED",
        "claimedAmount": 1000000}))          # approvedAmount·reducedAmount 미동봉(데이터결함)
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1               # DEAD 아님(정보성 보류)
    assert not await session.scalar(select(Voucher).where(Voucher.source_ref == "claim:review:C-PH"))


async def test_claim_reviewed_rejected_full_reduction(session):
    """REJECTED + 금액 미동봉 → 전액 삭감(claimed 전액 손실)은 정당(폴백 허용)."""
    from erp.finance.models import Voucher, VoucherLine
    session.add(_ev("finance", "claim.reviewed", "EVT-CRRJ", {
        "claimId": "C-RJ", "status": "REJECTED", "claimedAmount": 300000}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "claim:review:C-RJ"))
    loss = await session.scalar(select(VoucherLine.amount).where(
        VoucherLine.voucher_id == v.id, VoucherLine.account_code == "5910"))
    assert round(float(loss)) == 300000       # 전액 삭감손실


async def test_claim_reviewed_reduced_capped_at_claimed(session):
    """reducedAmount>claimedAmount(오류) → 청구액으로 상한(AR 음수·손실 과대 방지)."""
    from erp.finance.models import Voucher, VoucherLine
    session.add(_ev("finance", "claim.reviewed", "EVT-CRCAP", {
        "claimId": "C-CAP", "status": "REJECTED",
        "claimedAmount": 100000, "reducedAmount": 9990000}))    # 청구 초과 오류값
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "claim:review:C-CAP"))
    loss = await session.scalar(select(VoucherLine.amount).where(
        VoucherLine.voucher_id == v.id, VoucherLine.account_code == "5910"))
    assert round(float(loss)) == 100000       # 청구액 상한


async def test_billing_settled_correction_conflict_surfaces_dead(session):
    """§1 — 일마감 정정 재수신(같은 날짜·다른 금액)은 무음 유실 대신 DEAD로 가시화."""
    from erp.finance.models import Voucher, VoucherLine
    session.add(_ev("finance", "billing.settled", "evt-settle-A",
                    {"date": "2026-06-10", "summary": {"byPaymentMethod": [
                        {"paymentMethod": "CASH", "_sum": {"paidAmount": 500000}}]}}))
    await session.flush()
    await consumer.consume(session)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "settle:2026-06-10"))
    d = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    assert float(d.amount) == 500000
    # 정정본(800k)·다른 이벤트 ref → 처리 시 금액 불일치로 DEAD
    session.add(_ev("finance", "billing.settled", "evt-settle-B",
                    {"date": "2026-06-10", "summary": {"byPaymentMethod": [
                        {"paymentMethod": "CASH", "_sum": {"paidAmount": 800000}}]}}))
    await session.flush()
    await consumer.consume(session)
    ev2 = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == "evt-settle-B"))
    assert ev2.status == "DEAD" and "불일치" in (ev2.error or "")
    # 기존 전표 불변(무음 덮어쓰기 없음)
    d2 = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    assert float(d2.amount) == 500000


async def test_billing_settled_same_amount_idempotent(session):
    """§1 — 같은 날짜·같은 금액 재수신은 멱등 정상 처리(APPLIED, 이중전기 없음)."""
    from erp.finance.models import Voucher
    for ref in ("evt-s1", "evt-s2"):
        session.add(_ev("finance", "billing.settled", ref,
                        {"date": "2026-06-11", "summary": {"byPaymentMethod": [
                            {"paymentMethod": "CASH", "_sum": {"paidAmount": 300000}}]}}))
        await session.flush()
        await consumer.consume(session)
    from sqlalchemy import func
    n = await session.scalar(select(func.count()).select_from(Voucher).where(
        Voucher.source_ref == "settle:2026-06-11"))
    assert n == 1
    ev2 = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == "evt-s2"))
    assert ev2.status == "APPLIED"


async def test_claim_reviewed_correction_conflict_surfaces_dead(session):
    """§2 — 명시 revision=1 재전송(같은 청구·다른 삭감액)은 여전히 DEAD로 가시화.

    2026-10-09 시나리오 S-B: revision 없는 재심사(HIS 실 형태)는 이제 정정으로 추론된다
    (tests/test_scenario_claims_fixes.py) — 무음 유실 가드는 **명시 v1 재전송**에 대해 유지."""
    session.add(_ev("finance", "claim.submitted", "claim:submit:CY1",
                    {"claimId": "CY1", "claimNumber": "CY1", "claimedAmount": 1000000,
                     "careMonth": "2026-07", "submittedAt": "2026-07-10"}))
    await session.flush()
    await consumer.consume(session)
    session.add(_ev("finance", "claim.reviewed", "evt-rev-A",
                    {"claimId": "CY1", "status": "REDUCED", "reducedAmount": 100000,
                     "revision": 1}))
    await session.flush()
    await consumer.consume(session)
    # 같은 v1을 다른 금액(삭감 300k)·다른 이벤트 ref로 재전송 → 불일치로 DEAD
    session.add(_ev("finance", "claim.reviewed", "evt-rev-B",
                    {"claimId": "CY1", "status": "REDUCED", "reducedAmount": 300000,
                     "revision": 1}))
    await session.flush()
    await consumer.consume(session)
    ev2 = await session.scalar(select(HisInboundEvent).where(
        HisInboundEvent.source_ref == "evt-rev-B"))
    assert ev2.status == "DEAD" and "불일치" in (ev2.error or "")


async def test_billing_adjusted_card_refund_routes_to_1132(session):
    """H-4 준비 — 환불 method=CARD면 카드미수(1132) 대변(현금 시재 미변동), 없으면 현행 현금(1110)."""
    from erp.finance.models import Voucher, VoucherLine
    # CARD 환불 → 1132
    session.add(_ev("finance", "billing.adjusted", "adj-card",
                    {"billingId": "B-C", "type": "REFUND", "amount": 50000,
                     "paymentMethod": "CARD"}))
    await session.flush()
    await consumer.consume(session)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref.like("adjust:B-C:REFUND:%")))
    cr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "CREDIT"))
    assert cr.account_code == "1132"
    # method 없음 → 현행 현금(1110·하위호환)
    session.add(_ev("finance", "billing.adjusted", "adj-cash",
                    {"billingId": "B-K", "type": "CANCEL", "amount": 30000}))
    await session.flush()
    await consumer.consume(session)
    v2 = await session.scalar(select(Voucher).where(Voucher.source_ref.like("adjust:B-K:CANCEL:%")))
    cr2 = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v2.id, VoucherLine.side == "CREDIT"))
    assert cr2.account_code == "1110"


async def test_claim_reviewed_correction_version_reverses_and_reposts(session):
    """#2 — claim.reviewed correctionVersion=2면 직전 삭감 역분개 후 재전기(순효과=신금액·감사추적 보존)."""
    from sqlalchemy import func as _f

    from erp.finance.models import Voucher, VoucherLine
    session.add(_ev("finance", "claim.submitted", "claim:submit:CZ1",
                    {"claimId": "CZ1", "claimNumber": "CZ1", "claimedAmount": 1000000,
                     "careMonth": "2026-07", "submittedAt": "2026-07-10"}))
    await session.flush()
    await consumer.consume(session)
    # 최초 심사 삭감 100k
    session.add(_ev("finance", "claim.reviewed", "evt-rv1",
                    {"claimId": "CZ1", "status": "REDUCED", "reducedAmount": 100000}))
    await session.flush()
    await consumer.consume(session)
    # 정정 v2 삭감 300k
    session.add(_ev("finance", "claim.reviewed", "evt-rv2",
                    {"claimId": "CZ1", "status": "REDUCED", "reducedAmount": 300000,
                     "correctionVersion": 2}))
    await session.flush()
    assert (await consumer.consume(session))["applied"] >= 1
    # 원본(100k)·역분개(-100k)·정정(300k) 3전표 존재
    refs = set((await session.scalars(select(Voucher.source_ref).where(
        Voucher.source_module == "his.claim"))).all())
    assert {"claim:review:CZ1", "claim:review:CZ1:rev2", "claim:review:CZ1:v2"} <= refs
    # 순 삭감손실(5910) 차변 = 100k − 100k(역분개 대변) + 300k = 300k
    loss_debit = await session.scalar(
        select(_f.coalesce(_f.sum(VoucherLine.amount), 0)).select_from(VoucherLine)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.source_module == "his.claim", VoucherLine.account_code == "5910",
               VoucherLine.side == "DEBIT"))
    loss_credit = await session.scalar(
        select(_f.coalesce(_f.sum(VoucherLine.amount), 0)).select_from(VoucherLine)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.source_module == "his.claim", VoucherLine.account_code == "5910",
               VoucherLine.side == "CREDIT"))
    assert int(loss_debit) - int(loss_credit) == 300000    # 순 삭감손실 = 정정 후 값


async def test_billing_settled_correction_version_net_effect(session):
    """#2 — billing.settled correctionVersion=2면 일마감 전체 역분개 후 재전기(순 수익=신금액)."""
    from sqlalchemy import func as _f

    from erp.finance.models import Voucher, VoucherLine
    session.add(_ev("finance", "billing.settled", "evt-st1",
                    {"date": "2026-06-20", "summary": {"byPaymentMethod": [
                        {"paymentMethod": "CASH", "_sum": {"paidAmount": 500000}}]}}))
    await session.flush()
    await consumer.consume(session)
    session.add(_ev("finance", "billing.settled", "evt-st2",
                    {"date": "2026-06-20", "correctionVersion": 2,
                     "summary": {"byPaymentMethod": [
                         {"paymentMethod": "CASH", "_sum": {"paidAmount": 800000}}]}}))
    await session.flush()
    await consumer.consume(session)
    # 순 수익(4110) 대변 = 500k − 500k(역분개) + 800k = 800k
    rev_credit = await session.scalar(
        select(_f.coalesce(_f.sum(VoucherLine.amount), 0)).select_from(VoucherLine)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.source_module == "his.billing", VoucherLine.account_code == "4110",
               VoucherLine.side == "CREDIT"))
    rev_debit = await session.scalar(
        select(_f.coalesce(_f.sum(VoucherLine.amount), 0)).select_from(VoucherLine)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.source_module == "his.billing", VoucherLine.account_code == "4110",
               VoucherLine.side == "DEBIT"))
    assert int(rev_credit) - int(rev_debit) == 800000


async def test_correction_version_idempotent_resend(session):
    """#2 — 같은 correctionVersion 재수신은 멱등(역분개·재전기 이중 없음)."""
    from sqlalchemy import func as _f

    from erp.finance.models import Voucher
    for ref in ("evt-c1", "evt-c1b"):     # v2 두 번(다른 이벤트)
        session.add(_ev("finance", "billing.settled", ref,
                        {"date": "2026-06-21", "correctionVersion": 2,
                         "summary": {"byPaymentMethod": [
                             {"paymentMethod": "CASH", "_sum": {"paidAmount": 200000}}]}}))
        await session.flush()
        await consumer.consume(session)
    n = await session.scalar(select(_f.count()).select_from(Voucher).where(
        Voucher.source_ref == "settle:2026-06-21:v2"))
    assert n == 1


async def test_correction_via_his_revision_field(session):
    """#2 HIS 계약 정합 — correctionOf+revision(HIS) 필드로도 정정 동작(correctionVersion 아님)."""
    from sqlalchemy import func as _f

    from erp.finance.models import Voucher, VoucherLine
    session.add(_ev("finance", "billing.settled", "evt-hr1",
                    {"date": "2026-06-22", "summary": {"byPaymentMethod": [
                        {"paymentMethod": "CASH", "_sum": {"paidAmount": 400000}}]}}))
    await session.flush()
    await consumer.consume(session)
    # HIS 필드: correctionOf(원본 참조) + revision=2
    session.add(_ev("finance", "billing.settled", "evt-hr2",
                    {"date": "2026-06-22", "correctionOf": "settle:2026-06-22", "revision": 2,
                     "summary": {"byPaymentMethod": [
                         {"paymentMethod": "CASH", "_sum": {"paidAmount": 700000}}]}}))
    await session.flush()
    await consumer.consume(session)
    refs = set((await session.scalars(select(Voucher.source_ref).where(
        Voucher.source_module == "his.billing"))).all())
    assert "settle:2026-06-22:v2" in refs and "settle:2026-06-22:rev2" in refs
    rev_credit = await session.scalar(
        select(_f.coalesce(_f.sum(VoucherLine.amount), 0)).select_from(VoucherLine)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.source_module == "his.billing", VoucherLine.account_code == "4110",
               VoucherLine.side == "CREDIT"))
    rev_debit = await session.scalar(
        select(_f.coalesce(_f.sum(VoucherLine.amount), 0)).select_from(VoucherLine)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.source_module == "his.billing", VoucherLine.account_code == "4110",
               VoucherLine.side == "DEBIT"))
    assert int(rev_credit) - int(rev_debit) == 700000     # 순 수익 = 정정 후


async def test_certificate_issued_posts_to_4120(session):
    """HIS e결재 — 제증명 수입이 4120 제증명수입 대변(진료수익 4110과 분리)·결제수단별 차변·멱등."""
    from erp.finance.models import Account, Voucher, VoucherLine
    session.add_all([Account(code="1110", name="현금", kind="ASSET"),
                     Account(code="1132", name="카드미수", kind="ASSET"),
                     Account(code="4120", name="제증명수입", kind="REVENUE")])
    await session.flush()
    session.add(_ev("finance", "certificate.issued", "cert-evt-1",
                    {"certificateId": "CERT-1", "amount": 20000, "paymentMethod": "CARD",
                     "certificateType": "진단서", "issuedAt": "2026-07-19"}))
    await session.flush()
    await consumer.consume(session)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "cert:CERT-1"))
    cr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "CREDIT"))
    dr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    assert cr.account_code == "4120" and float(cr.amount) == 20000
    assert dr.account_code == "1132"     # CARD → 카드미수
    # 멱등 — 재수신 이중전기 없음
    session.add(_ev("finance", "certificate.issued", "cert-evt-1b",
                    {"certificateId": "CERT-1", "amount": 20000, "paymentMethod": "CARD"}))
    await session.flush()
    await consumer.consume(session)
    from sqlalchemy import func as _f
    n = await session.scalar(select(_f.count()).select_from(Voucher).where(
        Voucher.source_ref == "cert:CERT-1"))
    assert n == 1


async def test_preauth_order_approved_records_no_gl(session):
    """HIS e결재 — 사전승인 고가오더가 관리회계 기록(GL 미전기)·멱등 갱신."""
    from sqlalchemy import func as _f

    from erp.finance.models import PreauthOrder, Voucher
    session.add(_ev("procurement", "preauth.order_approved", "pre-evt-1",
                    {"orderRef": "PO-HV-1", "category": "고가검사", "amount": 5000000,
                     "deptCode": "RD", "approver": "원장", "approvedAt": "2026-07-18"}))
    await session.flush()
    await consumer.consume(session)
    row = await session.scalar(select(PreauthOrder).where(PreauthOrder.order_ref == "PO-HV-1"))
    assert row is not None and row.category == "고가검사" and float(row.amount) == 5000000
    # GL 전기 없음(약정 단계)
    vn = await session.scalar(select(_f.count()).select_from(Voucher).where(
        Voucher.source_module.like("%preauth%")))
    assert vn == 0
    # 멱등 — 재수신 시 최신값 갱신(행 증가 없음)
    session.add(_ev("procurement", "preauth.order_approved", "pre-evt-1b",
                    {"orderRef": "PO-HV-1", "category": "고가검사", "amount": 5500000}))
    await session.flush()
    await consumer.consume(session)
    rows = (await session.scalars(select(PreauthOrder).where(
        PreauthOrder.order_ref == "PO-HV-1"))).all()
    assert len(rows) == 1 and float(rows[0].amount) == 5500000


async def test_eapproval_document_issued_posts_to_4120(session):
    """보완 — eapproval.document_issued(필수 연계경로)도 4120 제증명수입 전기(fee 필드·문서id 수용)."""
    from erp.finance.models import Account, Voucher, VoucherLine
    session.add_all([Account(code="1110", name="현금", kind="ASSET"),
                     Account(code="1132", name="카드미수", kind="ASSET"),
                     Account(code="4120", name="제증명수입", kind="REVENUE")])
    await session.flush()
    session.add(_ev("finance", "eapproval.document_issued", "doc-evt-1",
                    {"documentId": "DOC-1", "fee": 15000, "paymentMethod": "CASH",
                     "documentType": "소견서", "issuedAt": "2026-07-19"}))
    await session.flush()
    await consumer.consume(session)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "cert:DOC-1"))
    cr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "CREDIT"))
    dr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    assert cr.account_code == "4120" and float(cr.amount) == 15000
    assert dr.account_code == "1110"     # CASH → 현금


async def test_eapproval_document_adjusted_reverses_revenue(session):
    """보완 — 발급 취소/환불(document_adjusted) → 4120 차변 역분개(수입 취소)·adjustmentId 멱등."""
    from sqlalchemy import func as _f

    from erp.finance.models import Account, Voucher, VoucherLine
    session.add_all([Account(code="1110", name="현금", kind="ASSET"),
                     Account(code="1132", name="카드미수", kind="ASSET"),
                     Account(code="4120", name="제증명수입", kind="REVENUE")])
    await session.flush()
    session.add(_ev("finance", "eapproval.document_adjusted", "docadj-evt-1",
                    {"documentId": "DOC-1", "adjustmentId": "ADJ-1", "refundAmount": 15000,
                     "paymentMethod": "CARD", "adjustedAt": "2026-07-20"}))
    await session.flush()
    await consumer.consume(session)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "cert-adj:ADJ-1"))
    dr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT"))
    cr = await session.scalar(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "CREDIT"))
    assert dr.account_code == "4120" and float(dr.amount) == 15000   # 수입 취소(차변)
    assert cr.account_code == "1132"                                  # CARD 환불
    # 멱등
    session.add(_ev("finance", "eapproval.document_adjusted", "docadj-evt-1b",
                    {"adjustmentId": "ADJ-1", "refundAmount": 15000, "paymentMethod": "CARD"}))
    await session.flush()
    await consumer.consume(session)
    n = await session.scalar(select(_f.count()).select_from(Voucher).where(
        Voucher.source_ref == "cert-adj:ADJ-1"))
    assert n == 1


async def test_preauth_emergency_bypass_flags_pending(session):
    """보완 — 응급우회(emergency_bypass) → EMERGENCY_BYPASS·post_approved=False(사후승인 미결)·GL 미전기."""
    from sqlalchemy import func as _f

    from erp.finance.models import PreauthOrder, Voucher
    session.add(_ev("procurement", "preauth.emergency_bypass", "byp-evt-1",
                    {"orderRef": "PO-EM-1", "category": "고가검사", "orderType": "마약류",
                     "amount": 300000, "reason": "응급 투여", "bypassedAt": "2026-07-20"}))
    await session.flush()
    await consumer.consume(session)
    row = await session.scalar(select(PreauthOrder).where(PreauthOrder.order_ref == "PO-EM-1"))
    assert row.channel == "EMERGENCY_BYPASS" and row.post_approved is False
    assert row.order_type == "마약류"
    vn = await session.scalar(select(_f.count()).select_from(Voucher).where(
        Voucher.source_module.like("%preauth%")))
    assert vn == 0
    # 사후승인 이벤트(order_approved 동일 ref) → post_approved=True 갱신
    session.add(_ev("procurement", "preauth.order_approved", "byp-evt-1b",
                    {"orderRef": "PO-EM-1", "category": "고가검사", "amount": 300000}))
    await session.flush()
    await consumer.consume(session)
    row2 = await session.scalar(select(PreauthOrder).where(PreauthOrder.order_ref == "PO-EM-1"))
    assert row2.post_approved is True and row2.channel == "STANDARD"



# ── 2026-10-09 전체 흐름 검토 A·F — HIS 실제 페이로드 형태 ─────────────────────

async def test_billing_settled_real_his_shape_posts_revenue(session):
    """HIS dailyClose 실제 형태 {summary:{data:{byPaymentMethod:{card,cash,unknown}}}} — 종전 '수납 0' 무음 skip."""
    from sqlalchemy import func, select

    from erp.finance.models import Voucher, VoucherLine
    session.add(_ev("finance", "billing.settled", "EVT-REAL1", {
        "date": "2026-10-08", "summary": {"data": {
            "date": "2026-10-08", "totalRevenue": 1_350_000,
            "byPaymentMethod": {"card": 1_000_000, "cash": 300_000, "unknown": 50_000}}}}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "settle:2026-10-08"))
    assert v is not None
    card = await session.scalar(select(func.sum(VoucherLine.amount)).where(
        VoucherLine.voucher_id == v.id, VoucherLine.account_code == "1132"))
    assert float(card) == 1_000_000


async def test_billing_settled_unparseable_with_revenue_goes_dead(session):
    """총수익>0인데 수단별 합 0 — 형태 변경을 APPLIED로 삼키지 않는다."""
    from sqlalchemy import select

    from erp.integrations.models import HisInboundEvent
    session.add(_ev("finance", "billing.settled", "EVT-REAL2", {
        "date": "2026-10-07", "summary": {"data": {"totalRevenue": 500_000, "byPaymentMethod": {}}}}))
    await session.flush()
    for _ in range(6):
        await consumer.consume(session)
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == "EVT-REAL2"))
    assert ev.status in ("FAILED", "DEAD")


async def test_multiple_partial_refunds_all_posted(session):
    """같은 수납의 부분환불 2건(revision 1·2) — 둘 다 전표(종전 2회차 무음 유실)."""
    from sqlalchemy import func, select

    from erp.finance.models import Voucher, VoucherLine
    for i, (amt, rev) in enumerate(((30_000, 1), (20_000, 2))):
        session.add(_ev("finance", "billing.adjusted", f"EVT-PR{i}", {
            "billingId": "B-PR", "type": "REFUND", "amount": amt, "revision": rev,
            "at": "2026-10-08T03:00:00.000Z"}))
    await session.flush()
    await consumer.consume(session)
    vids = (await session.scalars(select(Voucher.id).where(
        Voucher.source_ref.like("adjust:B-PR:REFUND:%")))).all()
    assert len(vids) == 2
    total = await session.scalar(select(func.sum(VoucherLine.amount)).where(
        VoucherLine.voucher_id.in_(vids), VoucherLine.side == "DEBIT"))
    assert float(total) == 50_000


async def test_refund_voucher_date_is_kst(session):
    """HIS toISOString(UTC) — KST 01:30 환불은 KST 날짜로(종전 앞 10자리 → 전날)."""
    from sqlalchemy import select

    from erp.finance.models import Voucher
    session.add(_ev("finance", "billing.adjusted", "EVT-KST", {
        "billingId": "B-KST", "type": "REFUND", "amount": 10_000, "revision": 1,
        "at": "2026-10-31T16:30:00.000Z"}))                 # = KST 2026-11-01 01:30
    await session.flush()
    await consumer.consume(session)
    v = await session.scalar(select(Voucher).where(Voucher.source_ref.like("adjust:B-KST:%")))
    assert v.voucher_on.isoformat() == "2026-11-01"
