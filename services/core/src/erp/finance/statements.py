"""재무제표·결산 (FR-08 확장) — 전표(VoucherLine) 집계로 시산표·손익계산서·재무상태표.

복식부기 정상잔액: ASSET·EXPENSE=차변, LIABILITY·REVENUE·자본=대변.
역분개는 원전표+반대전표가 모두 라인으로 남아 상계되므로 status 무관 전 라인 집계(정합).
자본 계정(kind) 부재 → 재무상태표 자본 = 자산−부채(이익잉여금≈누적순이익 포함).
"""
from datetime import date, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.finance.models import (
    Account, AccountBalanceCheckpoint, CorporateAccount, Voucher, VoucherLine,
)

_DEBIT = func.coalesce(func.sum(case((VoucherLine.side == "DEBIT", VoucherLine.amount),
                                      else_=0)), 0)
_CREDIT = func.coalesce(func.sum(case((VoucherLine.side == "CREDIT", VoucherLine.amount),
                                      else_=0)), 0)

# ── 현금흐름표 활동 분류 (간접법) — 계정코드 기준 ─────────────────────
# 항등식: 비결산 전표는 전표별 차=대 → Σ비현금(대−차) = ΔCash. 분류는 split만 좌우(총합 불변).
_CF_CASH = "1110"                              # 현금성자산(=현금흐름 대상, 기여계산서 제외)
# 유동 구분(재무비율) — 유동자산 11xx. 유동부채 = 21xx·23xx(미지급·선수·예수·단기차입) 중
# 비유동 충당·장기차입 제외. prefix만으론 2120 퇴직충당(비유동)·2310 단기차입(유동)이 오분류됨.
_CURRENT_ASSET_PREFIX = "11"
_CURRENT_LIAB_PREFIXES = ("21", "23")
_NONCURRENT_LIAB_CODES = {"2120", "2130", "2510"}   # 퇴직·기타충당(비유동)·장기차입금
_CF_OP_WC = {"1130", "1140", "1150", "2110"}   # 운전자본: 미수금·보험미수금·재고·미지급금
_CF_OP_NONCASH = {"1810", "1815", "1825", "2120"}  # 비현금 누계·충당(감가/손상/상각누계·퇴직충당) — 가산
_CF_INVESTING = {"1710", "1720"}               # 유형자산·사용권자산(취득/처분)
_CF_FINANCING = {"2210", "3300", "3400"}       # 리스부채·이익잉여금·재평가잉여금


def _cf_activity(code: str, kind: str | None) -> str:
    """계정 → 영업/투자/재무 활동 분류 (미매핑은 OPERATING 폴백+가시화)."""
    if code in _CF_OP_WC or code in _CF_OP_NONCASH:
        return "OPERATING"
    if code in _CF_INVESTING:
        return "INVESTING"
    if code in _CF_FINANCING:
        return "FINANCING"
    if kind == "ASSET":               # 기타 장기자산 — 투자 기본
        return "INVESTING"
    if kind in ("LIABILITY", "CAPITAL"):   # 기타 부채/자본 — 재무 기본
        return "FINANCING"
    return "UNMAPPED"


# ── G8 데이터 주도 분류(계정마스터 우선·하드코딩 폴백) ────────────────
def _is_current_acct(code: str, kind: str | None, cmap: dict) -> bool:
    """유동 여부 — accounts.is_current 우선(G8), NULL이면 하드코딩 prefix 폴백."""
    ic = cmap.get(code, (None, None))[0]
    if ic is not None:
        return bool(ic)
    if kind == "ASSET":
        return str(code).startswith(_CURRENT_ASSET_PREFIX)
    if kind == "LIABILITY":
        return (str(code).startswith(_CURRENT_LIAB_PREFIXES)
                and str(code) not in _NONCURRENT_LIAB_CODES)
    return False


def _cf_activity_of(code: str, kind: str | None, cmap: dict) -> str:
    """현금흐름 활동 — accounts.cf_activity 우선(G8), NULL이면 하드코딩 폴백."""
    cf = cmap.get(code, (None, None))[1]
    return cf if cf else _cf_activity(code, kind)


class StatementService:
    async def dimension_summary(self, session: AsyncSession, *, dim: str,
                                since: date, until: date) -> dict:
        """라인 확장 차원(G9)별 집계 — cost_center|project 기준 차·대·순액. 미지정(NULL) 라인 제외."""
        col = {"cost_center": VoucherLine.cost_center, "project": VoucherLine.project}[dim]
        rows = await session.execute(
            select(col, _DEBIT, _CREDIT).select_from(VoucherLine)
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(Voucher.status.in_(("POSTED", "REVERSED")),
                   Voucher.voucher_on >= since, Voucher.voucher_on <= until, col.isnot(None))
            .group_by(col).order_by(col))
        items = [{"value": v, "debit": float(d), "credit": float(cr),
                  "net": float(d) - float(cr)} for v, d, cr in rows]
        return {"dim": dim, "since": str(since), "until": str(until), "items": items}

    async def _class_map(self, session: AsyncSession) -> dict:
        """계정 → (is_current, cf_activity) — G8 데이터 주도 분류 오버레이(NULL=하드코딩 폴백)."""
        rows = await session.execute(
            select(Account.code, Account.is_current, Account.cf_activity))
        return {c: (ic, cf) for c, ic, cf in rows}

    async def _by_account(self, session: AsyncSession, *, until: date | None = None,
                          since: date | None = None, exclude_closing: bool = False):
        q = (select(VoucherLine.account_code, Account.name, Account.kind, _DEBIT, _CREDIT)
             .join(Voucher, Voucher.id == VoucherLine.voucher_id)
             .outerjoin(Account, Account.code == VoucherLine.account_code)
             # 방어선 — 미승인(DRAFT/PENDING)·반려(REJECTED) 전표 배제. 역분개는 원(REVERSED)+반전
             # (POSTED) 상계 위해 REVERSED 포함 필수. ix_voucher_status_on 인덱스 활용(비용 0).
             .where(Voucher.status.in_(("POSTED", "REVERSED"))))
        if exclude_closing:   # 손익은 결산분개 제외(수익/비용 상계 전 기간귀속). 재무상태표/시산표는 포함
            q = q.where(Voucher.source_module != "closing")
        if since is not None:
            q = q.where(Voucher.voucher_on >= since)
        if until is not None:
            q = q.where(Voucher.voucher_on <= until)
        q = q.group_by(VoucherLine.account_code, Account.name, Account.kind)
        return (await session.execute(q)).all()

    async def _by_account_as_of(self, session: AsyncSession, *, until: date):
        """계정별 누적(개시~until) — 체크포인트(있으면)+당기 델타로 O(전체)→O(당기). 미적재 시 전구간 폴백.

        성능 H-2. balance_sheet 전용(exclude_closing=False·POSTED/REVERSED와 동일 필터로 적재된 체크포인트).
        체크포인트+델타 == 전구간 스캔(verify_balance_checkpoint로 검증). 반환 형태는 _by_account와 동일
        (account_code, name, kind, debit, credit).
        """
        from decimal import Decimal
        cp_through = await session.scalar(
            select(func.max(AccountBalanceCheckpoint.through))
            .where(AccountBalanceCheckpoint.through <= until))
        if cp_through is None:
            return await self._by_account(session, until=until)   # 폴백 — 현행(전구간)
        merged: dict[str, list] = {}   # code -> [Decimal debit, Decimal credit]
        for code, cd, cc in (await session.execute(select(
                AccountBalanceCheckpoint.account_code, AccountBalanceCheckpoint.cum_debit,
                AccountBalanceCheckpoint.cum_credit)
                .where(AccountBalanceCheckpoint.through == cp_through))).all():
            merged[code] = [Decimal(cd), Decimal(cc)]
        nk: dict[str, tuple] = {}
        # 당기 델타 — 경계 다음날 ~ until (인덱스 활용·소량)
        for code, name, kind, d, cr in await self._by_account(
                session, since=cp_through + timedelta(days=1), until=until):
            merged.setdefault(code, [Decimal(0), Decimal(0)])
            merged[code][0] += d
            merged[code][1] += cr
            nk[code] = (name, kind)
        # 체크포인트에만 있고 델타 없는 계정 — Account에서 name/kind 보강(_by_account outerjoin 동형)
        missing = [c for c in merged if c not in nk]
        if missing:
            for code, name, kind in (await session.execute(select(
                    Account.code, Account.name, Account.kind)
                    .where(Account.code.in_(missing)))).all():
                nk[code] = (name, kind)
        return [(code, nk.get(code, (None, None))[0], nk.get(code, (None, None))[1], dc[0], dc[1])
                for code, dc in merged.items()]

    async def refresh_balance_checkpoint(self, session: AsyncSession, *, through: date) -> dict:
        """계정 잔액 체크포인트 적재(성능 H-2) — 직전 체크포인트 이후 델타만 누적(증분·최초는 전구간).

        경계(through)까지의 계정별 누적 차/대변합을 upsert. 증분이라 O(당기)·마감월 경계마다 배치 호출."""
        from decimal import Decimal
        prev = await session.scalar(
            select(func.max(AccountBalanceCheckpoint.through))
            .where(AccountBalanceCheckpoint.through < through))
        base: dict[str, list] = {}
        if prev is not None:
            for code, cd, cc in (await session.execute(select(
                    AccountBalanceCheckpoint.account_code, AccountBalanceCheckpoint.cum_debit,
                    AccountBalanceCheckpoint.cum_credit)
                    .where(AccountBalanceCheckpoint.through == prev))).all():
                base[code] = [Decimal(cd), Decimal(cc)]
        since = prev + timedelta(days=1) if prev is not None else None
        for code, _n, _k, d, cr in await self._by_account(session, since=since, until=through):
            base.setdefault(code, [Decimal(0), Decimal(0)])
            base[code][0] += d
            base[code][1] += cr
        existing = {r.account_code: r for r in (await session.scalars(
            select(AccountBalanceCheckpoint)
            .where(AccountBalanceCheckpoint.through == through))).all()}
        for code, (cd, cc) in base.items():
            row = existing.get(code)
            if row is None:
                session.add(AccountBalanceCheckpoint(through=through, account_code=code,
                                                     cum_debit=cd, cum_credit=cc))
            else:
                row.cum_debit, row.cum_credit = cd, cc
        await session.flush()
        return {"through": str(through), "accounts": len(base), "incremental": prev is not None}

    async def rebuild_balance_checkpoints(self, session: AsyncSession) -> dict:
        """체크포인트 전면 재적재 — 자기치유 (2026-10-09 전체 흐름 검토 D·HIGH-2).

        증분 적재는 `직전 체크포인트 + 증분`이라 한 번 틀어진 체크포인트(소급 전기가 전기 시점
        무효화와 배치 적재 사이에 끼어든 경합 등)를 다음 체크포인트가 그대로 물려받아 영구히 틀린다.
        검증 불일치 시 기존 경계(through)들을 오름차순으로 다시 쌓는다 — 각 경계를 이전 경계+구간
        합으로 재계산하므로 전체 비용은 원장 1회 스캔(O(전체))이고, 배치(off-peak)에서만 호출된다.
        재계산 결과에 없는 계정 행(소급 역분개로 사라진 계정)은 지운다."""
        from decimal import Decimal

        from sqlalchemy import delete
        throughs = sorted((await session.scalars(
            select(AccountBalanceCheckpoint.through).distinct())).all())
        cum: dict[str, list] = {}
        prev: date | None = None
        for th in throughs:
            since = prev + timedelta(days=1) if prev is not None else None
            for code, _n, _k, d, cr in await self._by_account(session, since=since, until=th):
                cum.setdefault(code, [Decimal(0), Decimal(0)])
                cum[code][0] += d
                cum[code][1] += cr
            existing = {r.account_code: r for r in (await session.scalars(
                select(AccountBalanceCheckpoint)
                .where(AccountBalanceCheckpoint.through == th))).all()}
            for code, (cd, cc) in cum.items():
                row = existing.pop(code, None)
                if row is None:
                    session.add(AccountBalanceCheckpoint(through=th, account_code=code,
                                                         cum_debit=cd, cum_credit=cc))
                else:
                    row.cum_debit, row.cum_credit = cd, cc
            if existing:
                await session.execute(delete(AccountBalanceCheckpoint).where(
                    AccountBalanceCheckpoint.id.in_([r.id for r in existing.values()]))
                    .execution_options(synchronize_session=False))
            prev = th
        await session.flush()
        return {"rebuilt": len(throughs)}

    async def verify_balance_checkpoint(self, session: AsyncSession, *, as_of: date) -> dict:
        """자기검증 — 체크포인트+델타 == 전구간 스캔(계정별 차/대변). 불일치 시 최초 상이 계정 보고."""
        fast = {c: (int(d), int(cr)) for c, _n, _k, d, cr
                in await self._by_account_as_of(session, until=as_of)}
        full = {c: (int(d), int(cr)) for c, _n, _k, d, cr
                in await self._by_account(session, until=as_of)}
        # 0 잔액(차=대=0) 계정은 양쪽 표현차 무시 — 순효과만 비교
        diff = sorted(c for c in set(fast) | set(full)
                      if fast.get(c, (0, 0)) != full.get(c, (0, 0)))
        return {"ok": not diff, "as_of": str(as_of), "accounts": len(full),
                "mismatched": diff[:10]}

    async def trial_balance(self, session: AsyncSession, *, since: date, until: date) -> dict:
        """시산표 — 계정별 차변·대변 합. 총차변=총대변(복식부기 검증)."""
        rows = await self._by_account(session, since=since, until=until)
        accounts = [{"account_code": c, "name": n, "kind": k,
                     "debit": float(d), "credit": float(cr)} for c, n, k, d, cr in rows]
        td = sum(a["debit"] for a in accounts)
        tc = sum(a["credit"] for a in accounts)
        # 원단위 정수 비교 (float 동등비교 회피 — 컬럼이 Numeric(*,0))
        return {"since": str(since), "until": str(until), "accounts": accounts,
                "total_debit": td, "total_credit": tc, "balanced": round(td) == round(tc)}

    async def income_statement(self, session: AsyncSession, *, since: date, until: date) -> dict:
        """손익계산서 — 기간 수익·비용·당기순이익. 결산분개 제외(마감 후 수익 0 붕괴 방지)."""
        rows = await self._by_account(session, since=since, until=until, exclude_closing=True)
        revenues, expenses = [], []
        for c, n, k, d, cr in rows:
            bal = float(cr) - float(d) if k == "REVENUE" else float(d) - float(cr)
            if k == "REVENUE":
                revenues.append({"account_code": c, "name": n, "amount": bal})
            elif k == "EXPENSE":
                expenses.append({"account_code": c, "name": n, "amount": bal})
        tr = sum(r["amount"] for r in revenues)
        te = sum(e["amount"] for e in expenses)
        # 기능별 분류(IAS 1) — 매출원가(COGS)·판관비(SGA)·기타, 매출총이익·영업이익
        fmap: dict[str, str | None] = {}
        codes = [e["account_code"] for e in expenses]
        if codes:
            fmap = {c: f for c, f in (await session.execute(
                select(Account.code, Account.cost_function).where(Account.code.in_(codes)))).all()}
        cogs = sum(e["amount"] for e in expenses if fmap.get(e["account_code"]) == "COGS")
        sga = sum(e["amount"] for e in expenses if fmap.get(e["account_code"]) == "SGA")
        other = te - cogs - sga
        return {"since": str(since), "until": str(until), "revenues": revenues,
                "expenses": expenses, "total_revenue": tr, "total_expense": te,
                "net_income": tr - te,
                "by_function": {"cogs": round(cogs), "sga": round(sga), "other": round(other),
                                "gross_profit": round(tr - cogs),
                                "operating_income": round(tr - cogs - sga)}}

    # 의료기관 회계기준 운영성과표 — 의료외(비-진료) 계정 분류(회계사 검증·조정 가능).
    # 의료외수익: 자산처분이익·재고조정·외환차익·손상환입·외화환산이익. 의료외비용: 처분손실·재평가손실·
    # 외환차손·외화환산손실·이자비용. 법인세는 별도 분리. 나머지 REVENUE/EXPENSE=의료수익/의료비용.
    _NON_MEDICAL_REVENUE = frozenset({"4610", "4615", "4620", "4630", "4640", "4641", "4650"})
    _NON_MEDICAL_EXPENSE = frozenset({"5610", "5616", "5630", "5650", "5660", "5670"})
    _TAX_EXPENSE = frozenset({"5810"})

    async def operating_statement(self, session: AsyncSession, *, since: date,
                                  until: date) -> dict:
        """의료기관 회계기준 운영성과표 — 의료수익−의료비용=의료이익 → 의료외손익 →
        법인세차감전순이익 → 법인세 → 당기순이익. 결산분개 제외(마감 후 붕괴 방지)."""
        rows = await self._by_account(session, since=since, until=until, exclude_closing=True)
        med_rev = med_exp = non_rev = non_exp = tax = 0.0
        items = {"medical_revenue": [], "medical_expense": [],
                 "non_medical_revenue": [], "non_medical_expense": [], "tax": []}
        for c, n, k, d, cr in rows:
            if k == "REVENUE":
                amt = float(cr) - float(d)
                if c in self._NON_MEDICAL_REVENUE:
                    non_rev += amt
                    items["non_medical_revenue"].append({"account_code": c, "name": n, "amount": amt})
                else:
                    med_rev += amt
                    items["medical_revenue"].append({"account_code": c, "name": n, "amount": amt})
            elif k == "EXPENSE":
                amt = float(d) - float(cr)
                if c in self._TAX_EXPENSE:
                    tax += amt
                    items["tax"].append({"account_code": c, "name": n, "amount": amt})
                elif c in self._NON_MEDICAL_EXPENSE:
                    non_exp += amt
                    items["non_medical_expense"].append({"account_code": c, "name": n, "amount": amt})
                else:
                    med_exp += amt
                    items["medical_expense"].append({"account_code": c, "name": n, "amount": amt})
        medical_income = med_rev - med_exp                    # 의료이익
        pretax = medical_income + non_rev - non_exp           # 법인세차감전순이익
        return {"since": str(since), "until": str(until),
                "medical_revenue": round(med_rev), "medical_expense": round(med_exp),
                "medical_income": round(medical_income),
                "non_medical_revenue": round(non_rev), "non_medical_expense": round(non_exp),
                "pretax_income": round(pretax), "income_tax": round(tax),
                "net_income": round(pretax - tax), "items": items,
                "note": "의료외 분류는 회계사 검증·조정 가능(statements._NON_MEDICAL_*)"}

    async def comparative_balance_sheet(self, session: AsyncSession, *, as_of: date) -> dict:
        """전기 비교 재무상태표(K-IFRS 1001) — 당기말·전기말 2기 병렬."""
        cur = await self.balance_sheet(session, as_of=as_of)
        # 전기 동일 일자 — 윤년 2/29 등 없는 날짜는 해당 월 말일로 보정
        from calendar import monthrange as _mr
        py = as_of.year - 1
        prior_date = date(py, as_of.month, min(as_of.day, _mr(py, as_of.month)[1]))
        prior = await self.balance_sheet(session, as_of=prior_date)
        return {"current": cur, "prior": prior,
                "delta": {"total_assets": cur["total_assets"] - prior["total_assets"],
                          "total_liabilities": cur["total_liabilities"] - prior["total_liabilities"],
                          "equity": cur["equity"] - prior["equity"]}}

    async def interim_report(self, session: AsyncSession, *, year: int, quarter: int) -> dict:
        """중간재무보고(K-IFRS 1034) — 분기 손익·누적(YTD)·전년 동분기 비교(요약)."""
        cur = await self.monthly_pl(session, year=year)
        prev = await self.monthly_pl(session, year=year - 1)
        months = [m for m in range(1, 13) if (m - 1) // 3 + 1 == quarter]
        ytd_months = list(range(1, quarter * 3 + 1))

        def _agg(rows, ms):
            sel = [r for i, r in enumerate(rows, start=1) if i in ms]
            rev = sum(r["revenue"] for r in sel)
            exp = sum(r["expense"] for r in sel)
            return {"revenue": round(rev), "expense": round(exp), "net": round(rev - exp)}
        return {"year": year, "quarter": quarter,
                "quarterly": _agg(cur, months), "cumulative": _agg(cur, ytd_months),
                "prior_quarter": _agg(prev, months)}

    async def comprehensive_income(self, session: AsyncSession, *, since: date,
                                   until: date) -> dict:
        """포괄손익계산서(K-IFRS 1001) — 당기순이익 + 기타포괄손익(OCI) = 총포괄손익.

        OCI = 기간 중 OCI 계정(재평가잉여금 3400·보험수리적손익 3410)의 순증감(대변−차변).
        """
        oci_accounts = [("3400", "재평가잉여금"), ("3410", "보험수리적손익")]
        inc = await self.income_statement(session, since=since, until=until)
        rows = {c: (float(d), float(cr)) for c, _n, _k, d, cr in
                await self._by_account(session, since=since, until=until)}
        oci_items, oci_total = [], 0.0
        for code, name in oci_accounts:
            d, cr = rows.get(code, (0.0, 0.0))
            mv = round(cr - d)        # 대변 증가 = OCI 이익(+), 차변 = OCI 손실(−)
            if mv:
                oci_items.append({"account_code": code, "name": name, "amount": mv})
                oci_total += mv
        return {"since": str(since), "until": str(until),
                "net_income": inc["net_income"], "oci_items": oci_items,
                "oci_total": round(oci_total),
                "total_comprehensive_income": round(inc["net_income"] + oci_total)}

    async def monthly_pl(self, session: AsyncSession, *, year: int) -> list[dict]:
        """월별 수익·비용·순이익 — 단일 쿼리(month×kind GROUP BY, N+1 회피)."""
        rows = (await session.execute(
            select(func.extract("month", Voucher.voucher_on), Account.kind, _DEBIT, _CREDIT)
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .outerjoin(Account, Account.code == VoucherLine.account_code)
            .where(Voucher.voucher_on >= date(year, 1, 1),   # sargable 연 범위(ix_voucher_status_on)
                   Voucher.voucher_on < date(year + 1, 1, 1),
                   Voucher.status.in_(("POSTED", "REVERSED")),   # 미승인·반려 배제(역분개 상계 유지)
                   Voucher.source_module != "closing")   # 결산분개 제외(월별 손익 붕괴 방지)
            .group_by(func.extract("month", Voucher.voucher_on), Account.kind))).all()
        agg = {m: {"revenue": 0.0, "expense": 0.0} for m in range(1, 13)}
        for mon, kind, d, c in rows:
            m = int(mon)
            if kind == "REVENUE":
                agg[m]["revenue"] += float(c) - float(d)
            elif kind == "EXPENSE":
                agg[m]["expense"] += float(d) - float(c)
        return [{"month": f"{year}-{m:02d}", "revenue": v["revenue"],
                 "expense": v["expense"], "net": v["revenue"] - v["expense"]}
                for m, v in sorted(agg.items())]

    async def balance_sheet(self, session: AsyncSession, *, as_of: date) -> dict:
        """재무상태표 — 기준일 누적. 자산=부채+자본, 자본=자산−부채(누적순이익 포함).

        누적은 체크포인트+델타(성능 H-2·미적재 시 전구간 폴백)로 산출 — 동일 결과, 대규모 시 스캔 축소."""
        rows = await self._by_account_as_of(session, until=as_of)
        assets, liabilities, capital = [], [], []
        retained = 0.0
        capital_total = 0.0
        unmapped: list[str] = []       # kind 미매핑(None/OTHER)·잔액 보유 → 재무제표서 조용히 탈락 방지
        for c, n, k, d, cr in rows:
            if k == "ASSET":
                assets.append({"account_code": c, "name": n, "amount": float(d) - float(cr)})
            elif k == "LIABILITY":
                liabilities.append({"account_code": c, "name": n, "amount": float(cr) - float(d)})
            elif k == "CAPITAL":          # 자본(이익잉여금 등) — 결산분개로 대체된 누적이익
                bal = float(cr) - float(d)
                capital.append({"account_code": c, "name": n, "amount": bal})
                capital_total += bal
            elif k == "REVENUE":
                retained += float(cr) - float(d)
            elif k == "EXPENSE":
                retained -= float(d) - float(cr)
            elif round(float(d) - float(cr)) != 0:   # 5종 kind 밖 + 잔액 보유 = 미분류(가시화)
                unmapped.append(c)
        ta = sum(a["amount"] for a in assets)
        tl = sum(x["amount"] for x in liabilities)
        # 자본 = 자산−부채로 산출, (이익잉여금 + 미결산 손익)과 대조해 복식부기 정합 검증.
        # 결산분개 후엔 손익이 이익잉여금으로 이전돼 retained→0·capital_total↑ (합 보존).
        # ⚠ balanced는 5종 kind 매핑분 항등식 — kind 미매핑 잔액 계정은 양변서 함께 탈락해 거짓 통과할
        # 수 있으므로 unmapped_accounts로 별도 가시화(cash_flow_statement와 동형). 있으면 재무제표 불완전.
        equity = ta - tl
        return {"as_of": str(as_of), "assets": assets, "liabilities": liabilities,
                "capital": capital, "total_assets": ta, "total_liabilities": tl, "equity": equity,
                "retained_earnings": retained, "capital_total": capital_total,
                "balanced": round(equity) == round(capital_total + retained) and not unmapped,
                "unmapped_accounts": sorted(set(unmapped))}

    async def equity_changes(self, session: AsyncSession, *, fiscal_year: int) -> dict:
        """기본금변동계산서 (의료기관 회계기준 규칙 서식, A-3).

        자본(CAPITAL) 계정별 기초잔액·당기 증가/감소·기말잔액 + 미처분 당기순손익 행.
        검증: 기초/기말 자본총계 = 같은 시점 재무상태표 equity와 일치(복식부기 정합).

        기관 회계연도(org_settings.fiscal_year_start_month) 적용 — financial_ratios·close_year·
        disclosure와 동일 창을 써야 재무제표 세트 정합이 유지됨(달력연도 하드코딩 시 자본변동
        당기순손익만 다른 기간이 돼 공시 수치 불일치·적대검토 HIGH).
        """
        from datetime import timedelta

        from erp.finance.fiscal import fiscal_range
        year_start, year_end = await fiscal_range(session, fiscal_year)   # 기관 회계연도(기본 역년)
        opening_until = year_start - timedelta(days=1)                    # 회계연도 개시 전일

        def _split(rows):
            cap, retained = {}, 0.0
            for c, n, k, d, cr in rows:
                if k == "CAPITAL":
                    cap[c] = {"name": n, "balance": float(cr) - float(d)}
                elif k == "REVENUE":
                    retained += float(cr) - float(d)
                elif k == "EXPENSE":
                    retained -= float(d) - float(cr)
            return cap, retained

        # 개시잔액은 개시이래 누적 → 체크포인트+델타로 서빙(성능·balance_sheet 동일 최적화).
        cap_open, retained_open = _split(await self._by_account_as_of(session, until=opening_until))
        cap_move, net_income = _split(await self._by_account(   # 당기 이동은 연 범위(기 최적)
            session, since=year_start, until=year_end))

        items = []
        codes = sorted(set(cap_open) | set(cap_move))
        for c in codes:
            opening = cap_open.get(c, {}).get("balance", 0.0)
            change = cap_move.get(c, {}).get("balance", 0.0)
            items.append({
                "account_code": c,
                "name": (cap_open.get(c) or cap_move.get(c))["name"],
                "opening": opening,
                "increase": change if change > 0 else 0.0,
                "decrease": -change if change < 0 else 0.0,
                "closing": opening + change})
        # 미처분 당기순손익 — 결산분개 전 누적손익(결산 후엔 이익잉여금 계정으로 이전돼 0)
        items.append({"account_code": "—", "name": "미처분 당기순손익(누적)",
                      "opening": retained_open,
                      "increase": net_income if net_income > 0 else 0.0,
                      "decrease": -net_income if net_income < 0 else 0.0,
                      "closing": retained_open + net_income})

        total_open = sum(i["opening"] for i in items)
        total_close = sum(i["closing"] for i in items)
        bs_open = await self.balance_sheet(session, as_of=opening_until)
        bs_close = await self.balance_sheet(session, as_of=year_end)
        # 점검 M4 — BS equity 비교는 같은 retained 로직과 동행(자기참조 위험).
        # 독립 검산: 기말까지 원장 전체 차변합=대변합(복식부기 전제)을 별도 확인.
        all_rows = await self._by_account(session, until=year_end)
        td = sum(float(d) for _c, _n, _k, d, _cr in all_rows)
        tc = sum(float(cr) for _c, _n, _k, _d, cr in all_rows)
        ledger_balanced = round(td) == round(tc)
        return {"fiscal_year": fiscal_year, "items": items,
                "net_income": net_income,
                "total_opening": total_open, "total_closing": total_close,
                "ledger_balanced": ledger_balanced,
                "balanced": (ledger_balanced
                             and round(total_open) == round(bs_open["equity"])
                             and round(total_close) == round(bs_close["equity"]))}

    async def financial_ratios(self, session: AsyncSession, *, year: int) -> dict:
        """재무비율·경영 KPI (재무 강화 P1-⑤) — 재무상태표·손익으로 산출.

        유동/부채/자기자본비율 + 의료수익의료이익률·인건비율·재료비율(의료기관 특화).
        유동 구분은 계정 prefix(자산 11·부채 21=유동). 분모 0이면 None(미산출)."""
        from erp.finance.fiscal import fiscal_range
        since, as_of = await fiscal_range(session, year)   # 기관 회계연도(기본 역년)
        bs = await self.balance_sheet(session, as_of=as_of)
        inc = await self.income_statement(session, since=since, until=as_of)
        op = await self.operating_statement(session, since=since, until=as_of)
        cmap = await self._class_map(session)   # G8 — 계정마스터 우선 유동 분류(하드코딩 폴백)
        cur_assets = sum(a["amount"] for a in bs["assets"]
                         if _is_current_acct(str(a["account_code"]), "ASSET", cmap))
        cur_liab = sum(x["amount"] for x in bs["liabilities"]
                       if _is_current_acct(str(x["account_code"]), "LIABILITY", cmap))
        rev = inc["total_revenue"]
        labor = sum(e["amount"] for e in inc["expenses"]
                    if e["account_code"] in ("5310", "5320"))   # 급여·퇴직급여
        material = sum(e["amount"] for e in inc["expenses"]
                       if e["account_code"] == "5410")           # 재료비

        def _r(n: float, d: float) -> float | None:
            return round(n / d, 4) if d else None

        return {
            "year": year,
            "current_ratio": _r(cur_assets, cur_liab),           # 유동비율
            "debt_ratio": _r(bs["total_liabilities"], bs["equity"]),      # 부채비율
            "equity_ratio": _r(bs["equity"], bs["total_assets"]),         # 자기자본비율
            # 의료수익의료이익률 — 운영성과표 의료이익/의료수익(종전 순이익 분자는 비영업·법인세
            # 혼입으로 라벨과 불일치 — 계산검토 M-4)
            "operating_margin": _r(op["medical_income"], op["medical_revenue"] or rev),
            "labor_cost_ratio": _r(labor, rev),                  # 인건비율
            "material_cost_ratio": _r(material, rev),            # 재료비율
            "basis": {"current_assets": cur_assets, "current_liabilities": cur_liab,
                      "total_assets": bs["total_assets"],
                      "total_liabilities": bs["total_liabilities"], "equity": bs["equity"],
                      "revenue": rev, "net_income": inc["net_income"],
                      "medical_income": op["medical_income"],
                      "labor": labor, "material": material},
        }

    async def cash_flow_direct(self, session: AsyncSession, *, since: date, until: date) -> dict:
        """현금흐름표 (직접법, K-IFRS 1007) — 현금계정 상대계정 기준 영업 수취·지급 직접 분류.

        현금(1110) 전표별 현금증감을, 비현금 상대계정(최대 금액=주된 상대)의 성격으로 분류:
        수익/미수금→영업수취, 급여/퇴직→종업원지급, 기타비용/미지급→공급자·기타지급,
        유형/사용권→투자, 리스부채/자본→재무. 합계는 ΔCash와 정합(reconciled).

        ⚠️ 한계: 전표당 '주된(최대금액) 상대계정' 하나로 활동을 분류 → 영업+투자가 한 전표에 섞이면
        전액이 한 활동으로 몰린다(총합·reconciled는 정확, split은 근사). 라인 단위 배분은 후속.
        """
        cash_accts = await self._cash_accounts(session)
        cash_vids = (select(VoucherLine.voucher_id)
                     .where(VoucherLine.account_code.in_(cash_accts)).distinct().scalar_subquery())
        rows = (await session.execute(
            select(VoucherLine.voucher_id, VoucherLine.account_code, VoucherLine.side,
                   VoucherLine.amount, Account.kind)
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .outerjoin(Account, Account.code == VoucherLine.account_code)
            .where(VoucherLine.voucher_id.in_(cash_vids), Voucher.voucher_on >= since,
                   Voucher.voucher_on <= until, Voucher.status.in_(("POSTED", "REVERSED")),
                   Voucher.source_module != "closing"))).all()
        per: dict[int, dict] = {}
        for vid, code, side, amt, kind in rows:
            v = per.setdefault(vid, {"cash": 0.0, "others": []})
            a = float(amt)
            if code in cash_accts:      # 현금계정간 이동(원화↔외화)은 순증감으로 상쇄
                v["cash"] += a if side == "DEBIT" else -a     # +유입 −유출
            else:
                v["others"].append((abs(a), code, kind))
        buckets = {"고객수취": 0.0, "종업원지급": 0.0, "공급자기타지급": 0.0,
                   "투자활동": 0.0, "재무활동": 0.0}
        for v in per.values():
            cash = v["cash"]
            if not cash or not v["others"]:
                continue
            _, code, kind = max(v["others"], key=lambda x: x[0])   # 주된 상대계정
            if code in _CF_INVESTING:
                buckets["투자활동"] += cash
            elif code in _CF_FINANCING:
                buckets["재무활동"] += cash
            elif code in ("5310", "5320"):
                buckets["종업원지급"] += cash
            elif kind == "REVENUE" or code in ("1130", "1140"):
                buckets["고객수취"] += cash
            else:
                buckets["공급자기타지급"] += cash
        op = buckets["고객수취"] + buckets["종업원지급"] + buckets["공급자기타지급"]
        net_change = op + buckets["투자활동"] + buckets["재무활동"]
        opening = await self._cash_balance(session, as_of=since - timedelta(days=1),
                                           cash_accts=cash_accts)
        closing = await self._cash_balance(session, as_of=until, cash_accts=cash_accts)
        return {"since": str(since), "until": str(until),
                "operating": {k: round(buckets[k]) for k in
                              ("고객수취", "종업원지급", "공급자기타지급")},
                "operating_net": round(op), "investing_net": round(buckets["투자활동"]),
                "financing_net": round(buckets["재무활동"]), "net_change": round(net_change),
                "opening_cash": round(opening), "closing_cash": round(closing),
                "reconciled": round(opening + net_change) == round(closing)}

    async def _cash_accounts(self, session: AsyncSession) -> set[str]:
        """현금흐름 대상 현금성 계정 — 1110(원화 현금) + 법인 자금계좌 GL(외화예금 1115 등·활성).

        단일 1110만 인식하면 외화예금 등 타 현금계좌 유출입이 현금흐름표에서 통째로 누락되는데
        reconciled는 1110 자기완결이라 True로 은폐된다(다통화 오표시). CorporateAccount로 일반화.
        """
        codes = {code for code in (await session.execute(
            select(CorporateAccount.gl_account_code)
            .where(CorporateAccount.is_active.is_(True)))).scalars() if code}
        codes.add(_CF_CASH)
        return codes

    async def _cash_balance(self, session: AsyncSession, *, as_of: date,
                            cash_accts: set[str] | None = None) -> float:
        """현금성 계정 누적 잔액 = 차변−대변 (기준일 이하). cash_accts 미지정 시 조회."""
        accts = cash_accts if cash_accts is not None else await self._cash_accounts(session)
        d, c = (await session.execute(
            select(_DEBIT, _CREDIT)
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code.in_(accts),
                   Voucher.status.in_(("POSTED", "REVERSED")),   # 미승인·반려 배제
                   Voucher.voucher_on <= as_of))).one()
        return float(d) - float(c)

    async def cash_flow_statement(self, session: AsyncSession, *,
                                  since: date, until: date) -> dict:
        """현금흐름표 (간접법) — 영업/투자/재무 3분류 + 기초·기말현금 정합 자가검증.

        원리: 비결산 전표는 전표별 차=대이므로 Σ비현금계정(대변−차변) = ΔCash(현금 차변순증).
        따라서 모든 비현금 계정의 기간 기여(대변−차변)를 활동별로 분류하면 합계가 ΔCash와 일치
        (reconciled 보장). 영업은 당기순이익(손익계정 합) + 비현금가산·운전자본증감으로 표시.
        결산분개(P&L↔이익잉여금, 비현금) 제외 — 당기순이익을 영업에 유지(현금영향 0이라 정합 불변).

        ⚠️ 한계: 비현금 계정간 거래(예: 신용취득 자산↔미지급, 재평가 자산↔자본)는 두 활동에
        상쇄 배분돼 활동 split을 근사한다(총합·reconciled는 정확). 정밀 분개는 후속(직접법).
        """
        cash_accts = await self._cash_accounts(session)
        rows = await self._by_account(session, since=since, until=until, exclude_closing=True)
        cmap = await self._class_map(session)   # G8 — 계정마스터 우선 현금흐름 분류(하드코딩 폴백)
        net_income = 0.0
        op_adj: list[dict] = []
        investing: list[dict] = []
        financing: list[dict] = []
        unmapped: list[str] = []
        for c, n, k, d, cr in rows:
            if c in cash_accts:      # 전 현금계정(원화·외화) — 현금흐름 대상서 제외(ΔCash로 별도 산출)
                continue
            contrib = float(cr) - float(d)        # 현금 기여(대변−차변)
            if not contrib:
                continue
            if k in ("REVENUE", "EXPENSE"):       # 손익 → 당기순이익(영업)
                net_income += contrib
                continue
            act = _cf_activity_of(c, k, cmap)
            item = {"account_code": c, "name": n, "amount": contrib}
            if act == "INVESTING":
                investing.append(item)
            elif act == "FINANCING":
                financing.append(item)
            else:                                  # OPERATING + UNMAPPED(폴백)
                op_adj.append(item)
                if act == "UNMAPPED":
                    unmapped.append(c)
        op_total = net_income + sum(i["amount"] for i in op_adj)
        inv_total = sum(i["amount"] for i in investing)
        fin_total = sum(i["amount"] for i in financing)
        net_change = op_total + inv_total + fin_total
        opening = await self._cash_balance(session, as_of=since - timedelta(days=1),
                                           cash_accts=cash_accts)
        closing = await self._cash_balance(session, as_of=until, cash_accts=cash_accts)
        return {
            "since": str(since), "until": str(until),
            "operating": {"net_income": net_income, "adjustments": op_adj,
                          "subtotal": op_total},
            "investing": {"items": investing, "subtotal": inv_total},
            "financing": {"items": financing, "subtotal": fin_total},
            "net_change": net_change,
            "opening_cash": opening, "closing_cash": closing,
            "unmapped_accounts": sorted(set(unmapped)),
            # 정합 자가검증 — 기초현금 + 순증 == 기말현금 (이중분개 항등식)
            "reconciled": round(opening + net_change) == round(closing)}
