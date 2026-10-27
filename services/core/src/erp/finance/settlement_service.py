"""채권/채무·자금관리 (FR-09 확장) — 미수금/미지급금 + 수금/지급 전표 + aging + 자금수지.

미수 발생: 미수금(차)/수익(대), 수금: 현금(차)/미수금(대).
미지급 발생: 비용(차)/미지급(대), 지급: 미지급(차)/현금(대). 모두 재무제표에 반영.
"""
from datetime import date, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import CorporateAccount, Payable, Receivable, Voucher, VoucherLine
from erp.finance.service import VoucherLineDTO, VoucherService

ACC_CASH = "1110"     # 현금성자산(원화)


async def _cash_accounts(session: AsyncSession) -> set[str]:
    """자금수지 대상 현금성 계정 — 1110(원화) + 법인 자금계좌 GL(외화예금 1115 등·활성).

    단일 1110만 집계하면 외화예금 유출입이 자금수지·현금흐름 마트에서 누락(statements와 동일 갭).
    CorporateAccount로 일반화(statements._cash_accounts와 동형).
    """
    codes = {c for c in (await session.execute(
        select(CorporateAccount.gl_account_code)
        .where(CorporateAccount.is_active.is_(True)))).scalars() if c}
    codes.add(ACC_CASH)
    return codes
ACC_AR = "1130"       # 미수금
ACC_AP = "2110"       # 미지급금

# ── source_module별 as_of 수금 재구성기(의존성 역전) ──────────────────────────────
# 기본 채권(1130)은 ar-collect-{id} 전표로 receivable별 회수를 복원(_settled_as_of)하나, 환자
# 본인부담(1131)의 회수(CR 1131)는 일마감 전표에 일자단위로 집계돼 receivable별 GL 추적이 불가하고
# finance는 patient를 import할 수 없다(계층 경계). 그래서 회수 소스를 아는 도메인(patient)이
# 자신의 재구성기를 import 시 등록한다 — reconstruct=True aging이 전 AR 계정에 정확해진다.
# 재구성기: (session, [Receivable], as_of) -> {receivable_id: as_of시점 수금액}
_AR_COLLECTED_RECONSTRUCTORS: dict = {}


def register_ar_collected_reconstructor(source_module: str, fn) -> None:
    """source_module 채권의 as_of 수금 재구성기 등록(patient/__init__ 등에서 호출)."""
    _AR_COLLECTED_RECONSTRUCTORS[source_module] = fn


class SettlementService:
    def __init__(self, voucher_service: VoucherService | None = None):
        self._v = voucher_service or VoucherService()

    # ── 채권(미수금) ───────────────────────────────────────
    async def record_receivable(self, session: AsyncSession, *, party: str, amount: float,
                                billed_on: date, due_on: date, description: str | None = None,
                                revenue_account: str = "4110", source_module: str = "manual",
                                source_ref: str | None = None, created_by: int | None = None):
        r = Receivable(party=party, description=description, amount=amount, billed_on=billed_on,
                       due_on=due_on, source_module=source_module, source_ref=source_ref)
        session.add(r)
        await session.flush()
        await self._v.create(session, source_module="ar", source_ref=f"ar-bill-{r.id}",
                             voucher_on=billed_on, memo=f"미수 발생: {party}", created_by=created_by,
                             lines=[VoucherLineDTO(ACC_AR, "DEBIT", amount),
                                    VoucherLineDTO(revenue_account, "CREDIT", amount)])
        return r

    async def collect(self, session: AsyncSession, *, receivable_id: int, amount: float,
                      on: date, created_by: int | None = None):
        # 행 잠금 — 동시 수금의 잔액 lost-update·채번(seq) 경합을 직렬화(sqlite는 무시)
        r = await session.get(Receivable, receivable_id, with_for_update=True)
        if r is None:
            raise ValidationError("존재하지 않는 채권입니다")
        outstanding = float(r.amount) - float(r.collected_amount)
        if amount <= 0 or amount > outstanding:
            raise ValidationError("수금액이 미수 잔액을 초과합니다", {"outstanding": outstanding})
        new_total = float(r.collected_amount) + amount
        r.collected_amount = new_total
        r.status = "COLLECTED" if new_total >= float(r.amount) else "PARTIAL"
        seq = (await session.scalar(
            select(func.count()).select_from(Voucher).where(
                Voucher.source_module == "ar",
                Voucher.source_ref.like(f"ar-collect-{r.id}-%"))) or 0) + 1
        await self._v.create(session, source_module="ar",
                             source_ref=f"ar-collect-{r.id}-{seq}", voucher_on=on,
                             memo=f"수금: {r.party}", created_by=created_by,
                             lines=[VoucherLineDTO(ACC_CASH, "DEBIT", amount),
                                    VoucherLineDTO(ACC_AR, "CREDIT", amount)])
        return r

    # ── 채무(미지급금) ─────────────────────────────────────
    async def record_payable(self, session: AsyncSession, *, party: str, amount: float,
                             incurred_on: date, due_on: date, description: str | None = None,
                             expense_account: str = "5110", source_module: str = "manual",
                             source_ref: str | None = None, created_by: int | None = None,
                             party_ref: str | None = None):
        p = Payable(party=party, description=description, amount=amount, incurred_on=incurred_on,
                    due_on=due_on, source_module=source_module, source_ref=source_ref,
                    party_ref=party_ref)
        session.add(p)
        await session.flush()
        await self._v.create(session, source_module="ap", source_ref=f"ap-bill-{p.id}",
                             voucher_on=incurred_on, memo=f"미지급 발생: {party}",
                             created_by=created_by,
                             lines=[VoucherLineDTO(expense_account, "DEBIT", amount),
                                    VoucherLineDTO(ACC_AP, "CREDIT", amount)])
        return p

    async def pay(self, session: AsyncSession, *, payable_id: int, amount: float, on: date,
                  created_by: int | None = None):
        # 행 잠금 — 동시 지급의 잔액 lost-update·채번(seq) 경합을 직렬화(sqlite는 무시)
        p = await session.get(Payable, payable_id, with_for_update=True)
        if p is None:
            raise ValidationError("존재하지 않는 채무입니다")
        outstanding = float(p.amount) - float(p.paid_amount)
        if amount <= 0 or amount > outstanding:
            raise ValidationError("지급액이 미지급 잔액을 초과합니다", {"outstanding": outstanding})
        new_total = float(p.paid_amount) + amount
        p.paid_amount = new_total
        p.status = "PAID" if new_total >= float(p.amount) else "PARTIAL"
        seq = (await session.scalar(
            select(func.count()).select_from(Voucher).where(
                Voucher.source_module == "ap",
                Voucher.source_ref.like(f"ap-pay-{p.id}-%"))) or 0) + 1
        await self._v.create(session, source_module="ap",
                             source_ref=f"ap-pay-{p.id}-{seq}", voucher_on=on,
                             memo=f"지급: {p.party}", created_by=created_by,
                             lines=[VoucherLineDTO(ACC_AP, "DEBIT", amount),
                                    VoucherLineDTO(ACC_CASH, "CREDIT", amount)])
        return p

    # ── 분석 ───────────────────────────────────────────────
    async def aging(self, session: AsyncSession, *, as_of: date, kind: str = "AR",
                    source_module: str | None = None, reconstruct: bool = False) -> dict:
        """연령분석 — 미회수/미지급 잔액을 경과일 버킷으로. source_module 지정 시 해당 출처만
        (예: 'patient' 환자 본인부담 미수).

        기본(reconstruct=False): **현시점 상태** — status/collected_amount 그대로. as_of=오늘이면
        정확하고 빠름(UI·현재 aging용).
        reconstruct=True: **as_of 시점 재구성** — billed_on/incurred_on ≤ as_of 인 채권/채무만,
        수금/지급도 as_of 이하 전표만 반영(GL 저널로 복원). 과거 월 마트를 나중에 재갱신해도 그 달 말
        실잔액이 안정적(BI 추세 정확). 상태기반 소급 오염 제거(전체검토 2026-07-03)."""
        if reconstruct:
            items = await self._aging_items_as_of(session, as_of=as_of, kind=kind,
                                                  source_module=source_module)
        elif kind == "AR":
            # WRITTEN_OFF(개별 상각·0209)는 원장에서 제거된 채권 — aging 잔액 제외
            q = select(Receivable).where(
                Receivable.status.notin_(("COLLECTED", "WRITTEN_OFF")))
            if source_module is not None:
                q = q.where(Receivable.source_module == source_module)
            rows = (await session.execute(q)).scalars().all()
            items = [(r.due_on, float(r.amount) - float(r.collected_amount)) for r in rows]
        else:
            q = select(Payable).where(Payable.status != "PAID")
            if source_module is not None:
                q = q.where(Payable.source_module == source_module)
            rows = (await session.execute(q)).scalars().all()
            items = [(p.due_on, float(p.amount) - float(p.paid_amount)) for p in rows]
        buckets = {"current": 0.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
        for due, outstanding in items:
            overdue = (as_of - due).days
            key = ("current" if overdue <= 0 else "1-30" if overdue <= 30
                   else "31-60" if overdue <= 60 else "61-90" if overdue <= 90 else "90+")
            buckets[key] += outstanding
        return {"as_of": str(as_of), "kind": kind, "buckets": buckets,
                "total": sum(buckets.values())}

    async def _settled_as_of(self, session: AsyncSession, *, as_of: date,
                             account: str, ref_prefix: str) -> dict[int, float]:
        """as_of 이하 수금/지급 누계(원장별) — GL 저널 순증감으로 복원.

        수금전표 ar-collect-{id}-* (ACC_AR CREDIT), 지급전표 ap-pay-{id}-* (ACC_AP DEBIT).
        반전(REVERSED+반전 POSTED 쌍)은 반대 side로 자동 상계되도록 두 상태 모두 포함해 net 집계.
        source_ref에서 원장 id 파싱은 DB 이식성 위해 파이썬에서(전표당 1행 그룹핑 후)."""
        # 원장 감소 방향: AR=CREDIT(수금), AP=DEBIT(지급). 반대 side는 반전이라 음수 가산.
        reduce_side = "CREDIT" if account == ACC_AR else "DEBIT"
        signed = func.sum(case((VoucherLine.side == reduce_side, VoucherLine.amount),
                               else_=-VoucherLine.amount))
        rows = (await session.execute(
            select(Voucher.source_ref, signed)
            .join(VoucherLine, VoucherLine.voucher_id == Voucher.id)
            .where(Voucher.source_ref.like(f"{ref_prefix}%"),
                   Voucher.voucher_on <= as_of,
                   Voucher.status.in_(("POSTED", "REVERSED")),
                   VoucherLine.account_code == account)
            .group_by(Voucher.source_ref))).all()
        out: dict[int, float] = {}
        for ref, amt in rows:
            try:                                    # "ar-collect-{id}-{seq}[/reversal-…]"
                rid = int(ref.split("-")[2])
            except (IndexError, ValueError):
                continue
            out[rid] = out.get(rid, 0.0) + float(amt or 0)
        return out

    async def _aging_items_as_of(self, session: AsyncSession, *, as_of: date, kind: str,
                                 source_module: str | None) -> list[tuple[date, float]]:
        """as_of 시점 미결제 (due_on, outstanding) 목록 — 발생일 ≤ as_of·수금/지급도 as_of 이하만."""
        if kind == "AR":
            q = select(Receivable).where(Receivable.billed_on <= as_of)
            if source_module is not None:
                q = q.where(Receivable.source_module == source_module)
            rows = (await session.execute(q)).scalars().all()
            default_settled = await self._settled_as_of(session, as_of=as_of, account=ACC_AR,
                                                        ref_prefix="ar-collect-")
            # source_module별 회수 복원 — 등록된 재구성기(예: patient 1131) 우선, 없으면 기본(1130).
            by_mod: dict = {}
            for r in rows:
                by_mod.setdefault(r.source_module or "", []).append(r)
            settled = {}
            for mod, recs in by_mod.items():
                fn = _AR_COLLECTED_RECONSTRUCTORS.get(mod)
                if fn is not None:
                    settled.update(await fn(session, recs, as_of))
                else:
                    for r in recs:
                        settled[r.id] = default_settled.get(r.id, 0.0)
        else:
            q = select(Payable).where(Payable.incurred_on <= as_of)
            if source_module is not None:
                q = q.where(Payable.source_module == source_module)
            rows = (await session.execute(q)).scalars().all()
            settled = await self._settled_as_of(session, as_of=as_of, account=ACC_AP,
                                                 ref_prefix="ap-pay-")
        items: list[tuple[date, float]] = []
        for r in rows:
            # 개별 상각(0209) — 상각일 ≤ as_of면 원장에서 제거된 채권(제외). 상각 전 시점 재구성은 포함
            # (당시엔 실재 미수·과거 월 ECL 재현 정확).
            wo = getattr(r, "written_off_on", None)
            if getattr(r, "status", None) == "WRITTEN_OFF" and wo is not None and wo <= as_of:
                continue
            outstanding = float(r.amount) - settled.get(r.id, 0.0)
            if outstanding > 0.5:                   # 원 단위 반올림 잔여 무시
                items.append((r.due_on, outstanding))
        return items

    async def cash_flow(self, session: AsyncSession, *, since: date, until: date) -> dict:
        """자금수지 — 현금 계정(원화·외화예금) 전표의 유입(차변)·유출(대변), 출처별."""
        cash_accts = await _cash_accounts(session)
        rows = (await session.execute(
            select(VoucherLine.side, Voucher.source_module,
                   func.coalesce(func.sum(VoucherLine.amount), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code.in_(cash_accts),
                   Voucher.status.in_(("POSTED", "REVERSED")),   # 미승인(DRAFT/PENDING) 배제·역분개 상계(월 현금흐름과 정합)
                   Voucher.voucher_on >= since, Voucher.voucher_on <= until)
            .group_by(VoucherLine.side, Voucher.source_module))).all()
        inflow = outflow = 0.0
        by_module: dict[str, dict] = {}
        for side, mod, amt in rows:
            amt = float(amt)
            m = by_module.setdefault(mod, {"inflow": 0.0, "outflow": 0.0})
            if side == "DEBIT":
                inflow += amt
                m["inflow"] += amt
            else:
                outflow += amt
                m["outflow"] += amt
        return {"since": str(since), "until": str(until), "inflow": inflow,
                "outflow": outflow, "net": inflow - outflow, "by_module": by_module}

    async def _cash_opening(self, session: AsyncSession, *, cash_accts, until: date) -> dict:
        """현금성 계정 개시잔액(개시~until 순증 DR−CR) — 체크포인트+델타 서빙(성능).

        개시이래 전 현금라인 재집계 대신 `_by_account_as_of`(체크포인트+당기 델타·balance_sheet와
        동일 최적화·POSTED/REVERSED 동일 필터) 결과에서 현금 계정만 추림. 체크포인트 미적재 시 전구간
        폴백(현행 무변경). 반환 {account_code: net}."""
        from erp.finance.statements import StatementService
        cs = set(cash_accts)
        rows = await StatementService()._by_account_as_of(session, until=until)
        return {code: float(d) - float(cr) for code, _n, _k, d, cr in rows if code in cs}

    async def daily_cash(self, session: AsyncSession, *, on: date) -> dict:
        """자금일보 — 일자 기준 계정별 기초잔액→입금→출금→기말잔액 (내부운영 갭 감사 Tier2).

        실적이 기간합계(cash_flow)뿐이라 자금담당 일상 업무(일일 자금현황·시재 대사)가 안 되던 갭.
        계정 = 현금성(1110+활성 법인계좌 GL). 기초 = 전일까지 순증(DR−CR), 당일 유출입은 출처별 병기.
        """
        cash_accts = sorted(await _cash_accounts(session))
        # 기초잔액 — 전일(on−1)까지 순증. 체크포인트 서빙(개시이래 재집계 제거·성능 감사 HIGH)
        opening = await self._cash_opening(session, cash_accts=cash_accts,
                                           until=on - timedelta(days=1))
        # 당일 유출입 — 계정×방향(+출처별 병기)
        day_rows = (await session.execute(
            select(VoucherLine.account_code, VoucherLine.side, Voucher.source_module,
                   func.coalesce(func.sum(VoucherLine.amount), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code.in_(cash_accts),
                   Voucher.status.in_(("POSTED", "REVERSED")),
                   Voucher.voucher_on == on)
            .group_by(VoucherLine.account_code, VoucherLine.side, Voucher.source_module))).all()
        per_acct: dict[str, dict] = {c: {"inflow": 0.0, "outflow": 0.0} for c in cash_accts}
        by_module: dict[str, dict] = {}
        for acct, side, mod, amt in day_rows:
            amt = float(amt)
            key = "inflow" if side == "DEBIT" else "outflow"
            per_acct[acct][key] += amt
            by_module.setdefault(mod, {"inflow": 0.0, "outflow": 0.0})[key] += amt
        accounts = []
        for c in cash_accts:
            op = opening.get(c, 0.0)
            i, o = per_acct[c]["inflow"], per_acct[c]["outflow"]
            accounts.append({"account_code": c, "opening": op, "inflow": i,
                             "outflow": o, "closing": op + i - o})
        t_open = sum(a["opening"] for a in accounts)
        t_in = sum(a["inflow"] for a in accounts)
        t_out = sum(a["outflow"] for a in accounts)
        return {"on": on.isoformat(), "accounts": accounts, "by_module": by_module,
                "opening": t_open, "inflow": t_in, "outflow": t_out,
                "closing": t_open + t_in - t_out}

    async def cash_forecast(self, session: AsyncSession, *, as_of: date,
                            months: int = 6) -> dict:
        """자금수지 예측 — 현금잔액 + 미수금 수금예정·미지급금 지급예정(due_on)으로 월별 포지션 추정.

        연체(due < 당월)는 0번 버킷(연체/당월). 예측지평 초과분은 제외.
        """
        months = max(1, min(months, 24))
        # 기초 현금잔액 — 현금성 계정(원화 1110 + 법인 외화예금 등) 잔액. cash_flow와 동일 코드셋.
        cash_accts = await _cash_accounts(session)
        # 기초 현금잔액(개시~as_of 순증) — 체크포인트 서빙(개시이래 재집계 제거·성능 감사 HIGH)
        opening = sum((await self._cash_opening(
            session, cash_accts=cash_accts, until=as_of)).values())
        ar = (await session.execute(   # 감사 F1 — 개별상각(WRITTEN_OFF)은 미래 유입 아님(aging과 정합)
            select(Receivable.due_on, Receivable.amount - Receivable.collected_amount)
            .where(Receivable.status.notin_(("COLLECTED", "WRITTEN_OFF"))))).all()
        ap = (await session.execute(
            select(Payable.due_on, Payable.amount - Payable.paid_amount)
            .where(Payable.status != "PAID"))).all()
        buckets = [{"inflow": 0.0, "outflow": 0.0} for _ in range(months + 1)]

        def _idx(due: date) -> int | None:
            k = (due.year - as_of.year) * 12 + (due.month - as_of.month)
            if k <= 0:
                return 0
            return k if k <= months else None

        for due, amt in ar:
            i = _idx(due)
            if i is not None:
                buckets[i]["inflow"] += float(amt or 0)
        for due, amt in ap:
            i = _idx(due)
            if i is not None:
                buckets[i]["outflow"] += float(amt or 0)
        running = opening
        out = []
        for i, b in enumerate(buckets):
            net = b["inflow"] - b["outflow"]
            running += net
            ym = (as_of.year * 12 + as_of.month - 1 + i)
            label = "연체·당월" if i == 0 else f"{ym // 12}-{ym % 12 + 1:02d}"
            out.append({"label": label, "inflow": b["inflow"], "outflow": b["outflow"],
                        "net": net, "balance": running})
        return {"as_of": str(as_of), "opening_cash": opening, "projection": out,
                "min_balance": min(p["balance"] for p in out)}

    async def aging_by_party(self, session: AsyncSession, *, as_of: date,
                             kind: str = "AP") -> dict:
        """거래처별 연령분석 매트릭스 — 미결제 채무/채권을 (거래처 × 경과일 버킷)으로.

        그룹키 = party_ref(거래처 마스터 안정키) 있으면 그것, 없으면 party(이름) 폴백. 총계 버킷만이던
        aging을 거래처 차원으로 확장(감사 발견: party×버킷 매트릭스·거래처별 채무 관리 부재)."""
        model = Payable if kind == "AP" else Receivable
        paid_col = model.paid_amount if kind == "AP" else model.collected_amount
        # 감사 F2 — AR은 개별상각(WRITTEN_OFF)도 제외(aging과 정합·거래처별 매트릭스 왜곡 방지)
        done = ("PAID",) if kind == "AP" else ("COLLECTED", "WRITTEN_OFF")
        rows = (await session.scalars(
            select(model).where(model.status.notin_(done)))).all()
        buckets_tpl = ("current", "1-30", "31-60", "61-90", "90+")
        by_party: dict = {}
        for r in rows:
            outstanding = float(r.amount) - float(getattr(r, paid_col.key))
            if outstanding <= 0.5:
                continue
            key = (getattr(r, "party_ref", None) or None), r.party
            slot = by_party.setdefault(key, {"party": r.party,
                                             "party_ref": getattr(r, "party_ref", None),
                                             "buckets": dict.fromkeys(buckets_tpl, 0.0),
                                             "total": 0.0, "next_due": None})
            overdue = (as_of - r.due_on).days
            b = ("current" if overdue <= 0 else "1-30" if overdue <= 30
                 else "31-60" if overdue <= 60 else "61-90" if overdue <= 90 else "90+")
            slot["buckets"][b] += outstanding
            slot["total"] += outstanding
            if slot["next_due"] is None or r.due_on < slot["next_due"]:
                slot["next_due"] = r.due_on
        items = sorted(by_party.values(), key=lambda x: x["total"], reverse=True)
        for it in items:
            it["next_due"] = it["next_due"].isoformat() if it["next_due"] else None
        return {"as_of": str(as_of), "kind": kind, "parties": items,
                "total": sum(it["total"] for it in items)}

    async def list_open(self, session: AsyncSession, *, kind: str = "AR",
                        page: int = 1, size: int = 50) -> dict:
        model = Receivable if kind == "AR" else Payable
        total = await session.scalar(
            select(func.count()).select_from(select(model).subquery())) or 0
        rows = (await session.scalars(
            select(model).order_by(model.due_on)
            .offset((page - 1) * size).limit(size))).all()
        if kind == "AR":
            items = [{"id": r.id, "party": r.party, "description": r.description,
                      "amount": float(r.amount), "collected": float(r.collected_amount),
                      "outstanding": float(r.amount) - float(r.collected_amount),
                      "due_on": r.due_on, "status": r.status} for r in rows]
        else:
            items = [{"id": p.id, "party": p.party, "description": p.description,
                      "amount": float(p.amount), "paid": float(p.paid_amount),
                      "outstanding": float(p.amount) - float(p.paid_amount),
                      "due_on": p.due_on, "status": p.status} for p in rows]
        return {"items": items, "total": total, "page": page, "size": size}
