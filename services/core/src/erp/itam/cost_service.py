"""ITAM M2b — 구독·클라우드 비용 원장 + 월 주기 점검·변동 감지 (기획 §5-⑤⑥·G-8/G-9)."""
import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.itam.models import CostReview, ITBudget, ServiceCost, SoftwareService

_audit = AuditService()
logger = logging.getLogger("erp.itam.cost")
_DEFAULT_THRESHOLD = 0.30      # 전월 대비 ±30% 이상 변동 시 플래그
# GL 전기 계정 매핑(결정) — IT 서비스 반복 운영비. 면세기관 매입세액 불공제 → 전액 비용(VAT 미분리).
_ACC_IT_OPEX = "5525"          # 정보시스템운영비(신규 판관비·마이그 0185)
_ACC_PAYABLE = "2110"          # 미지급금(발생주의·후불)
# 정기 비용이 발생하는 과금 유형(월 점검 대상). 영구(ONE_TIME)는 제외.
_RECURRING_BILLING = ("SEAT_RECURRING", "USAGE_METERED")


def _prev_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    m -= 1
    if m == 0:
        m, y = 12, y - 1
    return f"{y:04d}-{m:02d}"


def _norm_period(period: str) -> str:
    d = period.replace("-", "")
    if len(d) != 6 or not d.isdigit():
        raise ValidationError("period는 YYYYMM 또는 YYYY-MM")
    if not (1 <= int(d[4:6]) <= 12):      # S-8 — 월 범위 검증(13월 등 → run_review 500 방지)
        raise ValidationError("월은 01~12 범위여야 합니다")
    return f"{d[:4]}-{d[4:6]}"


class CostService:
    async def record_cost(self, session: AsyncSession, *, actor: int, service_id: int,
                          period: str, amount: float, currency: str = "KRW",
                          fx_rate: float | None = None, usage_note: str | None = None,
                          source: str = "MANUAL") -> ServiceCost:
        """월 비용 입력(upsert by service_id+period). 외화는 fx_rate로 KRW 환산 보관."""
        if await session.get(SoftwareService, service_id) is None:
            raise ValidationError("존재하지 않는 서비스입니다")
        ym = _norm_period(period)
        if amount < 0 or amount > 1e13:      # S-8 — 금액 범위(음수·overflow 방지)
            raise ValidationError("비용 금액이 유효 범위를 벗어났습니다")
        if currency != "KRW" and not fx_rate:
            raise ValidationError("외화 비용은 환율(fx_rate)이 필수입니다 — KRW 환산 집계 왜곡 방지")
        krw = round(amount * fx_rate) if currency != "KRW" else round(amount)
        row = await session.scalar(select(ServiceCost).where(
            ServiceCost.service_id == service_id, ServiceCost.period == ym))
        before = None
        if row is None:
            row = ServiceCost(service_id=service_id, period=ym, created_by=actor)
            session.add(row)
        else:
            # 재검토 HIGH — GL 전기 후 금액 수정은 원장↔GL 불일치 유발. 정정은 역분개(finance) 경유.
            if row.posted_at is not None and round(amount * (fx_rate or 1) if currency != "KRW"
                                                 else round(amount)) != int(row.amount_krw):
                raise ValidationError("이미 GL 전기된 비용은 수정할 수 없습니다(정정은 회계 역분개 후 재입력)")
            before = {"amount_krw": float(row.amount_krw)}   # S-6 — 동월 덮어쓰기 이전값 감사
        row.amount = amount
        row.currency = currency
        row.fx_rate = fx_rate
        row.amount_krw = krw
        row.usage_note = usage_note
        row.source = source
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="UPSERT",
                                  entity="itam.service_costs", target_id=str(service_id),
                                  before=before, after={"period": ym, "amount_krw": krw})
        return row

    async def costs(self, session: AsyncSession, service_id: int) -> list[ServiceCost]:
        return list((await session.scalars(select(ServiceCost).where(
            ServiceCost.service_id == service_id).order_by(ServiceCost.period.desc()))).all())

    # ── GL 전표 전기(R4·F-6) ──────────────────────────────────
    async def post_period(self, session: AsyncSession, *, actor: int, period: str) -> dict:
        """월 IT 비용 GL 전기 — 미전기 비용을 건별 전표로. DR 5525 / CR 2110. 멱등.

        건별 전표(source_ref=svc-cost:{id})라 나중 추가된 비용을 재실행 시 새로 전기(누락 없음).
        면세기관 매입세액 불공제 → amount_krw(부가세 포함) 전액 비용화(VAT 분리 없음).
        재검토 MED — 비용은 발생 사실이므로 서비스 해지 여부와 무관하게 전기(해지 후 유령·미전기화 방지).
        """
        from erp.finance.service import VoucherLineDTO, VoucherService
        ym = _norm_period(period)
        y, m = int(ym[:4]), int(ym[5:7])
        period_end = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
        rows = (await session.execute(
            select(ServiceCost, SoftwareService.name, SoftwareService.vendor,
                   SoftwareService.cost_center)
            .join(SoftwareService, SoftwareService.id == ServiceCost.service_id)
            .where(ServiceCost.period == ym))).all()
        posted = skipped = failed = 0
        total = 0
        for c, name, vendor, cost_center in rows:
            amt = int(c.amount_krw or 0)
            if c.posted_at is not None or amt <= 0:
                skipped += 1
                continue
            detail = " · ".join(x for x in (name, cost_center, vendor) if x)[:200]
            # 건별 savepoint 격리 — 1건 실패(마감월 등)가 전체 월 IT 비용 전기를 롤백하지 않게.
            try:
                async with session.begin_nested():
                    await VoucherService().create(
                        session, source_module="itam.cost", source_ref=f"svc-cost:{c.id}",
                        voucher_on=period_end, created_by=actor, memo=f"IT 비용 {ym} · {name}",
                        lines=[VoucherLineDTO(_ACC_IT_OPEX, "DEBIT", amt, detail=detail),
                               VoucherLineDTO(_ACC_PAYABLE, "CREDIT", amt, detail=detail)])
                    c.posted_at = datetime.now(timezone.utc)
                posted += 1
                total += amt
            except Exception:   # noqa: BLE001 — 건별 격리·다음 비용 진행
                failed += 1
                logger.warning("IT 비용 GL 전기 실패",
                               extra={"ctx": {"cost_id": c.id, "period": ym}})
        await _audit.record_audit(session, actor=actor, action="POST_GL",
                                  entity="itam.service_costs", target_id=ym,
                                  after={"posted": posted, "total_krw": total})
        return {"period": ym, "posted": posted, "skipped": skipped, "failed": failed,
                "total_krw": total, "account": _ACC_IT_OPEX}

    async def posting_status(self, session: AsyncSession, period: str) -> dict:
        """월 전기 현황 — 전기/미전기 건수·금액(대상=amount>0·서비스 상태 무관·전기 사실 기준)."""
        ym = _norm_period(period)
        rows = (await session.execute(
            select(ServiceCost.amount_krw, ServiceCost.posted_at)
            .where(ServiceCost.period == ym, ServiceCost.amount_krw > 0))).all()
        posted = sum(1 for _, p in rows if p is not None)
        unposted = sum(1 for _, p in rows if p is None)
        unposted_krw = float(sum(a for a, p in rows if p is None) or 0)
        posted_krw = float(sum(a for a, p in rows if p is not None) or 0)
        return {"period": ym, "posted": posted, "unposted": unposted,
                "unposted_krw": unposted_krw, "posted_krw": posted_krw}

    async def opex_summary(self, session: AsyncSession, period: str) -> dict:
        """월 IT 지출(OPEX) 집계 — 유형·부서(코스트센터)·벤더별 KRW 환산 (F-6 비용 후반)."""
        ym = _norm_period(period)
        total = float(await session.scalar(select(func.coalesce(func.sum(ServiceCost.amount_krw), 0))
                                           .where(ServiceCost.period == ym)) or 0)

        async def _group(col):
            rows = (await session.execute(
                select(col, func.sum(ServiceCost.amount_krw))
                .join(ServiceCost, ServiceCost.service_id == SoftwareService.id)
                .where(ServiceCost.period == ym).group_by(col))).all()
            # 미지정(NULL) 키는 "(미지정)"으로 표기 — 배분 누락 가시화
            return {(k if k not in (None, "") else "(미지정)"): float(v) for k, v in rows}

        return {"period": ym, "total_krw": total,
                "by_type": await _group(SoftwareService.service_type),
                "by_cost_center": await _group(SoftwareService.cost_center),
                "by_vendor": await _group(SoftwareService.vendor)}

    # ── Shadow-IT AP 대사(R4·F-6) — 미관리 IT 지출 탐지 ────────
    # IT성 지출 판정 키워드(품목/거래처명·설명). 병원 특성상 대소문자·한영 혼용 대응.
    _IT_KEYWORDS = ("software", "소프트웨어", "클라우드", "cloud", "saas", "라이선스", "license",
                    "구독", "subscription", "aws", "azure", "gcp", "google", "microsoft", "adobe",
                    "oracle", "오라클", "호스팅", "hosting", "도메인", "domain", "전산", "it서비스",
                    "시스템", "솔루션", "서버호스팅")
    # 법인격 접미어 — **토큰 단위**로만 제거(substring 과다제거·순서의존 오류 방지·재검토 LOW)
    _LEGAL_SUFFIX = frozenset({"주식회사", "유한회사", "co", "ltd", "inc", "corp", "corporation",
                               "llc", "gmbh", "kk", "company", "limited"})

    @classmethod
    def _norm_vendor(cls, name: str | None) -> str:
        import re
        v = (name or "").casefold().replace("㈜", "").replace("(주)", "")
        # 영숫자·한글 토큰화 후 법인격 접미어 토큰만 제거 → 결합(부분문자열 손상 없음)
        tokens = [t for t in re.findall(r"[0-9a-z가-힣]+", v) if t not in cls._LEGAL_SUFFIX]
        return "".join(tokens)

    @classmethod
    def _looks_it(cls, *texts: str | None) -> bool:
        blob = " ".join(t for t in texts if t).casefold()
        return any(k in blob for k in cls._IT_KEYWORDS)

    async def shadow_it_reconcile(self, session: AsyncSession, *, months: int = 12) -> dict:
        """Shadow-IT 대사 — 미지급금·전자세금계산서(매입) 중 ITAM 미등록 IT 벤더 지출 탐지.

        관리(managed)=ITAM 알려진 IT 벤더(SW서비스·벤더계약·유지보수)에 대한 지출(대사됨).
        후보(shadow)=IT성 키워드에 걸리나 알려진 IT 벤더가 아닌 지출(미관리 IT 후보·조사 대상).
        벤더 마스터 부재 → 정규화 문자열 매칭(오탐 가능·수동 검토 전제).
        """
        from erp.finance.models import Payable, TaxInvoice
        from erp.itam.models import MaintenanceRecord, VendorContract
        cutoff = date.today() - timedelta(days=max(30, months * 30))
        known = set()
        for col in (select(SoftwareService.vendor), select(VendorContract.vendor),
                    select(MaintenanceRecord.vendor)):
            for v in (await session.scalars(col)).all():
                nv = self._norm_vendor(v)
                if nv:
                    known.add(nv)

        managed, shadow = [], []
        # 미지급금(AP)
        aps = (await session.scalars(select(Payable).where(Payable.incurred_on >= cutoff))).all()
        for p in aps:
            rec = {"source": "AP", "id": p.id, "party": p.party, "description": p.description,
                   "amount": float(p.amount), "on": p.incurred_on, "source_module": p.source_module}
            if self._norm_vendor(p.party) in known:
                managed.append(rec)
            elif self._looks_it(p.party, p.description):
                shadow.append(rec)
        # 전자세금계산서(매입) — VOID 제외 + 미지급금 연결분(payable_id) 제외(이중집계 방지·재검토 MED)
        tis = (await session.scalars(select(TaxInvoice).where(
            TaxInvoice.direction == "IN", TaxInvoice.issue_date >= cutoff,
            TaxInvoice.status != "VOID", TaxInvoice.payable_id.is_(None)))).all()
        for t in tis:
            amt = float(t.supply_amount) + float(t.vat_amount or 0)
            rec = {"source": "TAX", "id": t.id, "party": t.supplier_name,
                   "description": None, "amount": amt, "on": t.issue_date, "source_module": "tax"}
            if self._norm_vendor(t.supplier_name) in known:
                managed.append(rec)
            elif self._looks_it(t.supplier_name):
                shadow.append(rec)

        managed.sort(key=lambda r: r["amount"], reverse=True)
        shadow.sort(key=lambda r: r["amount"], reverse=True)
        return {"months": months, "known_vendor_count": len(known),
                "managed_total": sum(r["amount"] for r in managed), "managed": managed,
                "shadow_total": sum(r["amount"] for r in shadow), "shadow": shadow}

    # ── IT 예산 관리(R4·F-6) ──────────────────────────────────
    async def upsert_budget(self, session: AsyncSession, *, actor: int, period: str,
                            cost_center: str | None, amount_krw: float,
                            note: str | None = None) -> ITBudget:
        """월 예산 설정(upsert by period+cost_center). cost_center 미지정=기관 IT 총예산."""
        ym = _norm_period(period)
        if amount_krw < 0 or amount_krw > 1e13:
            raise ValidationError("예산 금액이 유효 범위를 벗어났습니다")
        cc = (cost_center or "").strip() or None
        row = await session.scalar(select(ITBudget).where(
            ITBudget.period == ym,
            ITBudget.cost_center == cc if cc is not None else ITBudget.cost_center.is_(None)))
        before = None
        if row is None:
            row = ITBudget(period=ym, cost_center=cc, created_by=actor)
            session.add(row)
        else:
            before = {"amount_krw": float(row.amount_krw)}
        row.amount_krw = round(amount_krw)
        row.note = note
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="UPSERT",
                                  entity="itam.it_budgets", target_id=str(row.id),
                                  before=before, after={"period": ym, "cost_center": cc,
                                                        "amount_krw": row.amount_krw})
        return row

    async def delete_budget(self, session: AsyncSession, *, actor: int, budget_id: int) -> None:
        row = await session.get(ITBudget, budget_id)
        if row is None:
            raise ValidationError("예산 기록 없음")
        await _audit.record_audit(session, actor=actor, action="DELETE",
                                  entity="itam.it_budgets", target_id=str(budget_id),
                                  before={"period": row.period, "cost_center": row.cost_center,
                                          "amount_krw": float(row.amount_krw)})
        await session.delete(row)

    async def budget_variance(self, session: AsyncSession, period: str) -> dict:
        """월 예산 대비 실적 — 코스트센터별·전체. 초과(over)·미예산 지출(unbudgeted) 플래그(F-6)."""
        ym = _norm_period(period)
        summary = await self.opex_summary(session, ym)
        actual_cc = summary["by_cost_center"]          # {"영상의학": x, "(미지정)": y}
        actual_total = summary["total_krw"]
        budgets = list((await session.scalars(
            select(ITBudget).where(ITBudget.period == ym))).all())

        def _row(cc_label, budget, actual):
            over = actual > budget if budget else actual > 0
            pct = round((actual - budget) / budget * 100, 1) if budget else None
            return {"cost_center": cc_label, "budget_krw": float(budget) if budget is not None else None,
                    "actual_krw": actual, "variance_krw": actual - (budget or 0),
                    "over_pct": pct, "over": bool(over and budget)}

        rows, seen = [], set()
        total_budget = None
        for b in budgets:
            if b.cost_center is None:
                total_budget = float(b.amount_krw)
                continue
            seen.add(b.cost_center)
            rows.append(_row(b.cost_center, float(b.amount_krw), actual_cc.get(b.cost_center, 0.0)))
        # 예산 없이 지출만 있는 코스트센터(미예산 지출 가시화)
        for cc, amt in actual_cc.items():
            if cc not in seen and cc != "(미지정)":
                r = _row(cc, None, amt)
                r["unbudgeted"] = True
                rows.append(r)
        rows.sort(key=lambda r: r["actual_krw"], reverse=True)
        return {"period": ym, "total_budget_krw": total_budget, "total_actual_krw": actual_total,
                "total_over": bool(total_budget is not None and actual_total > total_budget),
                "rows": rows}

    async def list_budgets(self, session: AsyncSession, period: str) -> list[ITBudget]:
        ym = _norm_period(period)
        return list((await session.scalars(select(ITBudget).where(ITBudget.period == ym)
                     .order_by(ITBudget.cost_center.is_(None).desc(), ITBudget.cost_center))).all())

    async def ledger_csv(self, session: AsyncSession, *, period: str | None = None) -> str:
        """비용 원장 CSV(감사 제출용·F-10) — period 지정 시 해당 월, 없으면 전체. 서비스명 병기."""
        stmt = (select(ServiceCost, SoftwareService.name, SoftwareService.service_type,
                       SoftwareService.vendor)
                .join(SoftwareService, SoftwareService.id == ServiceCost.service_id))
        if period:
            stmt = stmt.where(ServiceCost.period == _norm_period(period))
        rows = (await session.execute(
            stmt.order_by(ServiceCost.period.desc(), ServiceCost.service_id))).all()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["기간", "서비스", "유형", "벤더", "청구금액", "통화", "환율",
                    "KRW환산", "사용량메모", "출처"])
        for c, name, stype, vendor in rows:
            w.writerow([c.period, name, stype, vendor or "", float(c.amount), c.currency,
                        float(c.fx_rate) if c.fx_rate else "", float(c.amount_krw),
                        c.usage_note or "", c.source])
        return buf.getvalue()

    # ── 주기 점검·변동 감지 ───────────────────────────────────
    async def run_review(self, session: AsyncSession, *, actor: int, period: str,
                         threshold: float = _DEFAULT_THRESHOLD) -> dict:
        """월 비용 점검 — 정기 서비스별 전월 대비 변동·미입력을 감지해 CostReview 플래그.

        확정(CONFIRMED)된 리뷰는 보존(재실행이 되돌리지 않음·카운터에서도 제외). 미입력=FLAGGED(MISSING).
        생애주기 필터: 점검월에 미개시(started_on 이후)·만료(expires_on 경과) 서비스는 제외 — 영구 MISSING 오탐 방지.
        """
        ym = _norm_period(period)
        prev = _prev_period(ym)
        y, m = int(ym[:4]), int(ym[5:7])
        month_start = date(y, m, 1)
        month_end = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
        services = (await session.scalars(select(SoftwareService).where(
            SoftwareService.billing_model.in_(_RECURRING_BILLING),
            SoftwareService.status == "ACTIVE",   # F-2 — 해지 서비스 점검 제외
            or_(SoftwareService.started_on.is_(None), SoftwareService.started_on <= month_end),
            or_(SoftwareService.expires_on.is_(None), SoftwareService.expires_on >= month_start),
        ))).all()
        # N+1 제거 — 서비스별 3쿼리(CostReview·당월/전월 ServiceCost)를 IN 일괄조회+사전맵으로(성능검토)
        ids = [s.id for s in services]
        reviews = {r.service_id: r for r in (await session.scalars(select(CostReview).where(
            CostReview.service_id.in_(ids), CostReview.period == ym))).all()} if ids else {}
        costs: dict[tuple[int, str], float] = {}
        if ids:
            for sid, per, amt in (await session.execute(select(
                    ServiceCost.service_id, ServiceCost.period, ServiceCost.amount_krw).where(
                    ServiceCost.service_id.in_(ids),
                    ServiceCost.period.in_((ym, prev))))).all():
                costs[(sid, per)] = amt
        flagged = missing = ok = confirmed = 0
        for s in services:
            # 확정분 보존 — 재산정·카운터 모두 제외(검토: 이중 계상 방지)
            rv = reviews.get(s.id)
            if rv is not None and rv.status == "CONFIRMED":
                confirmed += 1
                continue
            cur = costs.get((s.id, ym))
            pv = costs.get((s.id, prev))
            amount = float(cur) if cur is not None else None
            prev_amount = float(pv) if pv is not None else None
            delta = reason = None
            if amount is None:
                reason, status = "MISSING", "FLAGGED"
                missing += 1
            elif prev_amount and prev_amount > 0:
                delta = round((amount - prev_amount) / prev_amount * 100, 2)
                if abs(delta) >= threshold * 100:
                    reason = "SPIKE" if delta > 0 else "DROP"
                    status = "FLAGGED"
                    flagged += 1
                else:
                    status = "PENDING"
                    ok += 1
            else:
                status = "PENDING"
                ok += 1
            if rv is None:
                rv = CostReview(service_id=s.id, period=ym)
                session.add(rv)
            rv.prev_amount = prev_amount
            rv.amount = amount
            rv.delta_pct = delta
            rv.reason = reason
            rv.status = status
        await session.flush()
        out = {"period": ym, "flagged": flagged, "missing": missing, "ok": ok,
               "confirmed": confirmed, "total": len(services)}
        await _audit.record_audit(session, actor=actor, action="RUN",
                                  entity="itam.cost_reviews", target_id=ym, after=out)
        return out

    async def list_reviews(self, session: AsyncSession, *, period: str,
                           status: str | None = None) -> list[tuple[CostReview, SoftwareService]]:
        ym = _norm_period(period)
        stmt = (select(CostReview, SoftwareService)
                .join(SoftwareService, SoftwareService.id == CostReview.service_id)
                .where(CostReview.period == ym))
        if status:
            stmt = stmt.where(CostReview.status == status)
        return list((await session.execute(stmt.order_by(CostReview.status,
                                                         CostReview.delta_pct.desc()))).all())

    async def confirm_review(self, session: AsyncSession, *, actor: int, review_id: int,
                             note: str | None = None) -> None:
        rv = await session.get(CostReview, review_id)
        if rv is None:
            raise ValidationError("점검 기록 없음")
        if rv.status == "CONFIRMED":     # S-7 — 기확인 덮어쓰기 거부
            raise ValidationError("이미 확인된 점검입니다")
        before = {"status": rv.status}
        rv.status = "CONFIRMED"
        rv.reviewed_by = actor
        rv.reviewed_at = datetime.now(timezone.utc)
        if note:
            rv.note = note
        await _audit.record_audit(session, actor=actor, action="CONFIRM",
                                  entity="itam.cost_reviews", target_id=str(review_id),
                                  before=before, after={"note": note})
