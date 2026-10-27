"""진료비 책정 서비스 (D-6) — 라인 미러 × 시점 단가 × 분리 엔진 → 중간/최종 진료비.

interim=true: 재원/내원 누적 현재까지 = 중간 진료비. 기수납(HIS 수납 미러) 차감.
사전심사 BLOCK 여부 동봉(수납 전 심사팀 확인 신호 — D-6 요청서 §2).
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_

from erp.patient.billing_engine import compute_invoice
from erp.patient.models import BillingLine, Patient, Payment, Reception, Refund


class BillingService:
    async def _fee_map(self, session: AsyncSession, codes: set[str], as_of: date) -> dict:
        """진료일 기준 유효 단가 — 사전심사와 동일 형태 {price, cap, type}.

        H1: type을 잃으면 NONCOVERED_CLAIM·약가상한 룰이 invoice 경로에서 침묵
        (워크리스트와 신호 불일치) — 사전심사 엔진 fee 계약을 그대로 유지.
        """
        from erp.regulatory.models import FeeMaster
        if not codes:
            return {}
        rows = (await session.execute(
            select(FeeMaster.code, FeeMaster.unit_price, FeeMaster.price_cap,
                   FeeMaster.code_type, FeeMaster.benefit_category, FeeMaster.name,
                   FeeMaster.valid_from, FeeMaster.valid_to, FeeMaster.source)
            .where(FeeMaster.code.in_(codes), FeeMaster.valid_from <= as_of,
                   or_(FeeMaster.valid_to.is_(None), FeeMaster.valid_to > as_of)))).all()
        return {c: {"price": float(up or 0),
                    "cap": float(pc) if pc is not None else None, "type": ct,
                    "benefit_category": bc, "name": nm,
                    "valid_from": vf, "valid_to": vt, "source": src}
                for c, up, pc, ct, bc, nm, vf, vt, src in rows}

    async def _fee_map_current(self, session: AsyncSession, codes: set[str]) -> dict:
        """현행(valid_to NULL) 약가 — 매핑 폴백용(진료일에 약가 미시행 시).

        매핑은 약품 식별이므로, 진료일에 약가가 없으면 현행 약가로 단가 해소.
        """
        from erp.regulatory.models import FeeMaster
        if not codes:
            return {}
        rows = (await session.execute(
            select(FeeMaster.code, FeeMaster.unit_price, FeeMaster.price_cap,
                   FeeMaster.code_type, FeeMaster.benefit_category, FeeMaster.name)
            .where(FeeMaster.code.in_(codes), FeeMaster.valid_to.is_(None)))).all()
        return {c: {"price": float(up or 0),
                    "cap": float(pc) if pc is not None else None, "type": ct,
                    "benefit_category": bc, "name": nm} for c, up, pc, ct, bc, nm in rows}

    async def _prepaid(self, session: AsyncSession, *, chart_no: str,
                       since: date | None = None, care_month: str | None = None) -> float:
        """기수납 순액 — 환자(chart_no) 유효 수납(PAID) 합계 − 환불(과오납 반환). 스코프 정합.

        care_month 지정 시 그 진료월 수납만(외래·심사 명세) → invoice 스코프와 일치. 미지정 시 since
        이후(입원 재원 전체). 취소(CANCELED)는 이미 PAID서 제외돼 `−CANCELED` 이중차감 안 함.
        환불은 status를 뒤집지 않고 별도 Refund 행으로 남으므로(전체검토 HIGH), 여기서 순차감하지
        않으면 balance_due가 계속 음수(환불 대상)로 남아 유령 환불이 반복 가능(현금누수)."""
        q = (select(func.coalesce(func.sum(Payment.amount), 0))
             .join(Reception, Reception.id == Payment.reception_id)
             .join(Patient, Patient.id == Reception.patient_id)
             .where(Patient.chart_no == chart_no, Payment.status == "PAID"))
        # 환불은 chart_no·care_month가 수납과 대칭(마이그 0189)이라 동일 스코프로 순차감.
        rq = select(func.coalesce(func.sum(Refund.amount), 0)).where(Refund.chart_no == chart_no)
        if care_month is not None:
            q = q.where(Payment.care_month == care_month)
            rq = rq.where(Refund.care_month == care_month)
        elif since is not None:
            q = q.where(Payment.paid_on >= since)
            rq = rq.where(Refund.refunded_on >= since)
        paid = float(await session.scalar(q) or 0)
        refunded = float(await session.scalar(rq) or 0)
        if care_month is None:
            # 입원 중간정산 수납(HIS billing.interim_paid)도 기수납 — 종전엔 patient.payments만 세어 창구 balance_due가
            # 중간수납만큼 크게 나왔다(HIS 요청서 2026-10-22 R6). 환불(interim_refunded)은 순차감. 심사 명세(care_month)
            # 스코프는 외래·월 단위라 넣지 않는다.
            from erp.patient.models import InterimBilling, InterimBillingRefund
            iq = (select(func.coalesce(func.sum(InterimBilling.paid_amount), 0))
                  .where(InterimBilling.chart_no == chart_no, InterimBilling.status == "PAID"))
            irq = (select(func.coalesce(func.sum(InterimBillingRefund.amount), 0))
                   .join(InterimBilling, InterimBilling.interim_billing_id == InterimBillingRefund.interim_billing_id)
                   .where(InterimBilling.chart_no == chart_no, InterimBilling.status == "PAID"))
            if since is not None:
                # 이번 입원 회차만 — 기간 시작이 없으면(구 이벤트) 수납 시각으로
                from datetime import datetime, time

                from erp.common.kst import KST
                since_at = datetime.combine(since, time.min, tzinfo=KST)   # 입원일 KST 자정(세션 TZ 무관)
                in_stay = or_(InterimBilling.period_start >= since,
                              (InterimBilling.period_start.is_(None)) & (InterimBilling.paid_at >= since_at))
                iq, irq = iq.where(in_stay), irq.where(in_stay)
            paid += float(await session.scalar(iq) or 0)
            refunded += float(await session.scalar(irq) or 0)
        return paid - refunded

    async def invoice(self, session: AsyncSession, *, chart_no: str,
                      encounter_id: str | None = None, care_month: str | None = None,
                      interim: bool = True) -> dict:
        q = select(BillingLine).where(BillingLine.chart_no == chart_no,
                                      BillingLine.cancelled.is_(False))
        if encounter_id:
            q = q.where(BillingLine.encounter_id == encounter_id)
        if care_month:                       # 심사 명세 — 진료월 한정(YYYY-MM)
            d = care_month.replace("-", "")
            y, mo = int(d[:4]), int(d[4:6])
            nxt = date(y + (mo == 12), (mo % 12) + 1, 1)
            q = q.where(BillingLine.treated_on >= date(y, mo, 1),
                        BillingLine.treated_on < nxt)
        rows = (await session.scalars(q.order_by(BillingLine.treated_on,
                                                 BillingLine.id))).all()
        if not rows:
            return {"chart_no": chart_no, "encounter_id": encounter_id,
                    "available": False, "reason": "진료 라인 없음 (HIS 피드 대기 또는 미발생)"}

        as_of = max((r.treated_on for r in rows if r.treated_on), default=date.today())
        # 약품 코드 매핑(CONFIRMED) — HIS 비표준코드→보험코드 자동 변환(D-6 상시 레이어)
        from erp.regulatory.drug_map_service import DrugMapService
        cmap = await DrugMapService.confirmed_map(session)
        codes = {r.code for r in rows}
        codes |= {cmap[c] for c in codes if c in cmap}
        # HIS 동봉 보험코드(insuranceProductCode·자격검증분) — 단가 최우선(MASTER 직행)
        ipc = {r.insurance_product_code for r in rows if r.insurance_product_code}
        codes |= ipc
        fee = await self._fee_map(session, codes, as_of)
        map_fee = await self._fee_map_current(
            session, {cmap[c] for c in codes if c in cmap} | ipc)
        lines = []
        for r in rows:
            # 단가 우선순위: HIS 동봉 보험코드 → ERP 마스터(자기코드) → 매핑 보험코드 → HIS 폴백
            fm = fee.get(r.code) or {}
            master = fm.get("price")
            his = float(r.unit_price_his) if r.unit_price_his is not None else None
            ipc_fee = None
            if r.insurance_product_code:
                pc = r.insurance_product_code
                ipc_fee = fee.get(pc) if (fee.get(pc) or {}).get("price", 0) > 0 \
                    else map_fee.get(pc)
            mapped = None
            if r.code in cmap:
                ins = cmap[r.code]
                mapped = fee.get(ins) if (fee.get(ins) or {}).get("price", 0) > 0 \
                    else map_fee.get(ins)
            if ipc_fee and ipc_fee.get("price", 0) > 0:
                fm = ipc_fee                  # HIS 자격검증 보험코드 — 마스터 직행
                unit, source, priced = fm["price"], "MASTER", True
            elif master is not None and master > 0:
                unit, source, priced = master, "MASTER", True
            elif mapped and mapped.get("price", 0) > 0:
                fm = mapped                   # 매핑 보험코드 마스터로 분류·명칭 승계
                unit, source, priced = fm["price"], "MAP", True
            elif his is not None and his > 0:
                unit, source, priced = his, "HIS", True
            else:
                unit, source, priced = (master or 0.0), "NONE", r.code in fee
            bc = fm.get("benefit_category")
            if not bc:    # 마스터 미해소(HIS 폴백·신규 코드) — 런타임 분류
                from erp.patient.billing_classify import classify_benefit
                bc = classify_benefit(r.code_type, fm.get("name"))
            lines.append({"code": r.code, "count": float(r.count),
                          "unit_price": unit, "priced": priced, "price_source": source,
                          "code_type": r.code_type, "benefit_category": bc,
                          "name": fm.get("name"), "coverage": r.coverage,
                          # 약품 식별(병용금기·중복·성분중복 사전심사) — DRUG 라인만 값 존재
                          "drug_name": r.drug_name, "drug_ingredient": r.drug_ingredient,
                          "fee_effective_from": fm.get("valid_from"),
                          "fee_valid_to": fm.get("valid_to"),
                          "fee_source": fm.get("source"),
                          "encounter_type": r.encounter_type,
                          "treated_on": r.treated_on, "treated_at": r.treated_at,
                          "age": r.patient_age, "sex": r.patient_sex,   # 연령·성별 제한 수가 심사 보조
                          "admitted_on": r.admitted_on,
                          "special_case_type": r.special_case_type,
                          "medical_aid_class": r.medical_aid_class,
                          "his_copay_rate": float(r.his_copay_rate)
                          if r.his_copay_rate is not None else None,
                          "eligibility_status": r.eligibility_status,
                          "copay_category": r.copay_category})
        admitted = min((r.admitted_on for r in rows if r.admitted_on), default=None)
        # 기수납 스코프 정합 — 심사 명세(care_month)·외래 단일월 인카운터는 진료월로 좁혀 교차 인카운터
        # 과다상계 방지. 입원(재원 span)은 admitted 이후 전체(월 경계로 끊지 않음).
        scope_cm = care_month or (as_of.strftime("%Y-%m")
                                  if encounter_id and admitted is None else None)
        prepaid = await self._prepaid(session, chart_no=chart_no, since=admitted,
                                      care_month=scope_cm)
        from erp.config import get_settings
        from erp.patient.billing_rates import load_billing_rates
        rate_cfg = await load_billing_rates(session, as_of)  # 진료일 시점 기관 종별
        inv = compute_invoice(lines, prepaid=prepaid,
                              rates=rate_cfg["rates"], grade=rate_cfg["grade"],
                              round_unit=get_settings().patient_copay_round_unit)  # 10원 절사

        # C2: 매핑 약품(MAP)은 원본코드로도 보험코드 fee 노출 — 사전심사 약가상한·비급여 검증 동작
        pre_fee = dict(fee)
        for r in rows:
            if r.code in cmap and r.code not in pre_fee and cmap[r.code] in fee:
                pre_fee[r.code] = fee[cmap[r.code]]
        pre = await self._pre_review(session, rows, pre_fee)  # fee=사전심사 계약 {price,cap,type}
        return {"chart_no": chart_no, "encounter_id": encounter_id,
                "available": True, "interim": interim, "as_of": as_of,
                "admitted_on": admitted, **inv, "pre_review": pre}

    async def _pre_review(self, session: AsyncSession, rows, fee: dict) -> dict:
        """동일 라인으로 사전심사 — BLOCK이면 수납 전 심사팀 확인 신호.

        fee는 {price, cap, type} 원형 그대로 — NONCOVERED_CLAIM·약가상한 룰 동작(H1).
        """
        from erp.claims.models import PreReviewRule
        from erp.claims.pre_review import PreReviewEngine
        rules = (await session.scalars(
            select(PreReviewRule).where(PreReviewRule.active.is_(True)))).all()
        lines = [{"chart_no": r.chart_no, "code": r.code, "count": int(r.count),
                  "amount": (fee.get(r.code) or {}).get("price", 0.0) * float(r.count)
                  or None, "coverage": r.coverage,
                  "treated_on": r.treated_on, "treated_at": r.treated_at,
                  "age": r.patient_age, "sex": r.patient_sex} for r in rows]
        findings = PreReviewEngine().evaluate(lines=lines, rules=rules, fee=fee)
        return {"count": len(findings),
                "blocked": any(f.severity == "BLOCK" for f in findings)}
