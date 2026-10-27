"""재무제표 주석 (K-IFRS 1001) — 정량주석 자동 산출 + 정성주석 수기 편집.

자동: 유형자산 증감표(취득·감가·처분·장부가)·충당부채 증감표(기초·전입·감소·기말) — GL 계정 증감에서 산출.
수기: 회계정책 등 정성 주석(payload 텍스트). (period, note_key) upsert.
"""
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import (
    AccountingChange, Contingency, DisclosureNote, RelatedParty, RelatedPartyTxn,
    SubsequentEvent, Voucher, VoucherLine,
)

# 유형자산 취득원가·감가누계·충당부채 주석 대상 계정
PPE_COST = [("1710", "의료장비"), ("1720", "사용권자산")]
PPE_ACCUM_DEP = [("1810", "감가상각누계"), ("1825", "사용권상각누계")]
PROVISIONS = [("2120", "퇴직급여충당부채"), ("1190", "대손충당금"), ("1159", "재고자산평가충당금"),
              ("2130", "기타충당부채")]
ACC_PROVISION_OTHER = "2130"   # 기타충당부채(우발→충당 전환, K-IFRS 1037)
ACC_PROVISION_EXP = "5680"     # 충당부채전입액


class DisclosureService:
    async def _movements(self, session: AsyncSession, *, codes: list[str], start: date,
                         end: date) -> dict[str, tuple]:
        """여러 계정의 기간 증감을 1쿼리 일괄(N+1 제거). 미존재 계정은 호출부 .get(.,zeros)."""
        def _side(side, cond):
            return func.coalesce(func.sum(case(
                (VoucherLine.side == side, VoucherLine.amount), else_=0)).filter(cond), 0)
        before = Voucher.voucher_on < start
        during = (Voucher.voucher_on >= start) & (Voucher.voucher_on <= end)
        rows = (await session.execute(
            select(VoucherLine.account_code,
                   _side("DEBIT", before), _side("CREDIT", before),
                   _side("DEBIT", during), _side("CREDIT", during))
            .select_from(VoucherLine)
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code.in_(codes),
                   # 미승인·반려 수동전표 배제(statements 규약 — 계산검토 M-1)
                   Voucher.status.in_(("POSTED", "REVERSED")))
            .group_by(VoucherLine.account_code))).all()
        return {code: tuple(float(x) for x in rest) for code, *rest in rows}

    async def ppe_movement(self, session: AsyncSession, *, start: date, end: date) -> dict:
        """유형자산 증감표 — 취득원가·감가상각누계·장부가(K-IFRS 1016)."""
        mv = await self._movements(
            session, codes=[c for c, _ in PPE_COST] + [c for c, _ in PPE_ACCUM_DEP],
            start=start, end=end)
        cost_open = cost_acq = cost_disp = 0.0
        for code, _n in PPE_COST:
            bd, bcr, dd, dcr = mv.get(code, (0.0, 0.0, 0.0, 0.0))
            cost_open += bd - bcr
            cost_acq += dd
            cost_disp += dcr
        dep_open = dep_charge = dep_disp = 0.0
        for code, _n in PPE_ACCUM_DEP:
            bd, bcr, dd, dcr = mv.get(code, (0.0, 0.0, 0.0, 0.0))
            dep_open += bcr - bd
            dep_charge += dcr
            dep_disp += dd
        cost_close = cost_open + cost_acq - cost_disp
        dep_close = dep_open + dep_charge - dep_disp
        return {"cost": {"opening": round(cost_open), "acquisition": round(cost_acq),
                         "disposal": round(cost_disp), "closing": round(cost_close)},
                "accum_dep": {"opening": round(dep_open), "charge": round(dep_charge),
                              "disposal": round(dep_disp), "closing": round(dep_close)},
                "book_value": {"opening": round(cost_open - dep_open),
                               "closing": round(cost_close - dep_close)}}

    async def provision_movement(self, session: AsyncSession, *, start: date, end: date) -> dict:
        """충당부채/충당금 증감표 — 기초·전입·감소(환입/사용)·기말(K-IFRS 1037)."""
        items = []
        mv = await self._movements(session, codes=[c for c, _ in PROVISIONS], start=start, end=end)
        for code, name in PROVISIONS:
            bd, bcr, dd, dcr = mv.get(code, (0.0, 0.0, 0.0, 0.0))
            opening = bcr - bd
            items.append({"account_code": code, "name": name, "opening": round(opening),
                          "charge": round(dcr), "release": round(dd),
                          "closing": round(opening + dcr - dd)})
        return {"items": items}

    async def auto_notes(self, session: AsyncSession, *, year: int) -> list[dict]:
        from erp.finance.lease_service import LeaseService
        from erp.finance.fiscal import fiscal_range
        start, end = await fiscal_range(session, year)   # 기관 회계연도
        return [
            {"note_key": "ppe_movement", "title": "유형자산 증감 (K-IFRS 1016)", "auto": True,
             "payload": await self.ppe_movement(session, start=start, end=end)},
            {"note_key": "provision_movement", "title": "충당부채·충당금 증감 (K-IFRS 1037)",
             "auto": True, "payload": await self.provision_movement(session, start=start, end=end)},
            {"note_key": "lease_maturity", "title": "리스부채 만기분석 (K-IFRS 1116)", "auto": True,
             "payload": await LeaseService().maturity(session)},
            {"note_key": "fair_value", "title": "공정가치 서열 (K-IFRS 1113)", "auto": True,
             "payload": await self.fair_value_hierarchy(session)},
        ]

    async def fair_value_hierarchy(self, session: AsyncSession) -> dict:
        """공정가치 서열(K-IFRS 1113) — 재평가 자산을 측정수준(Level 1/2/3)별 장부가 집계."""
        from erp.finance.models import FixedAsset
        rows = (await session.execute(
            select(FixedAsset.fair_value_level, func.coalesce(func.sum(
                FixedAsset.acquisition_cost + FixedAsset.accumulated_revaluation
                - FixedAsset.accumulated_depreciation - FixedAsset.accumulated_impairment), 0),
                func.count())
            .where(FixedAsset.fair_value_level.isnot(None), FixedAsset.status != "DISPOSED")
            .group_by(FixedAsset.fair_value_level))).all()
        levels = [{"level": lv, "book_value": round(float(v)), "count": int(c)}
                  for lv, v, c in rows]
        return {"levels": sorted(levels, key=lambda x: x["level"]),
                "total": round(sum(x["book_value"] for x in levels))}

    async def notes(self, session: AsyncSession, *, year: int) -> dict:
        """주석 묶음 — 자동 정량주석 + 저장된 정성주석."""
        auto = await self.auto_notes(session, year=year)
        stored = (await session.execute(select(DisclosureNote).where(
            DisclosureNote.period == str(year)))).scalars().all()
        return {"year": year, "auto_notes": auto,
                "registry": await self.registry(session, year=year),
                "notes": [{"note_key": n.note_key, "title": n.title, "auto": False,
                           "payload": n.payload} for n in stored]}

    # ── 등록형 공시: 특수관계자(1024)·우발(1037)·후속사건(1010) ──────────
    # 기준별 시그니처 계정 — 해당 IFRS 측정·조정이 전기되면 움직이는 계정(당해 활동 집계용)
    STD_ACCOUNTS = {
        "1109": ["5690"],                       # 대손상각비(ECL)
        "1012": ["5810"],                       # 이연법인세비용
        "1002": ["5540"],                       # 재고평가손실
        "1019": ["3410"],                       # 퇴직 보험수리 재측정(OCI)
        "1036": ["5620", "5216", "4640", "4641"],  # 손상차손·환입(유형·무형)
        "1038": ["5215", "4615", "5616"],       # 무형 상각·처분손익
        "1021": ["4650", "5670"],               # 외화환산손익
        "1115": ["1135", "2155"],               # 계약자산(미청구수익)·환불부채(변동대가)
        "1037": ["5680"],                       # 충당부채전입
    }

    # IFRS 정합 카탈로그 — (분류, 기준, 명칭, 처리내용, 상태, 적용화면). 인앱 'IFRS 정합 현황' 화면용.
    CONFORMANCE_CATALOG = [
        ("측정", "1109", "금융상품 손상(대손 ECL)", "연령 매트릭스·forward-looking 수동 오버레이(거시모델 미연계)·결산분개", "✅", "/finance-periods"),
        ("측정", "1012", "법인세(이연법인세)", "일시적차이 DTA/DTL·유효세율", "🟡", "/finance-periods"),
        ("측정", "1002", "재고자산", "저가법(NRV)·원가흐름(FIFO/WAVG)·매출원가(COGS)", "✅", "/inventory-valuation"),
        ("측정", "1019", "종업원급여(퇴직)", "보험수리 DBO·재측정 OCI(3410)", "🟡", "/hr/retirement"),
        ("측정", "1016", "유형자산", "원가·감가상각·재평가모형(OCI)", "✅", "/assets"),
        ("측정", "1036", "자산손상", "회수가능액·손상/환입(원가·재평가모형)", "✅", "/assets"),
        ("측정", "1038", "무형자산", "정액상각·손상/환입·처분(생애주기)", "✅", "/assets"),
        ("측정", "1023", "차입원가", "차입원가 수동 자본화(자본화율·건설기간 게이트 미산정)", "🟡", "/assets"),
        ("측정", "1021", "환율변동효과", "외화잔액 기말환율 환산·환산차손익", "✅", "/finance-corporate-accounts"),
        ("측정", "1116", "리스", "사용권자산/리스부채·면제·재측정·만기분석", "✅", "/finance-leases"),
        ("측정", "1037", "충당부채·우발부채", "우발 등록 + 충당부채 전환(5680/2130)", "✅", "/finance-statements"),
        ("표시", "1001", "재무제표 표시", "포괄손익(당기+OCI)·기능별 비용·전기비교", "✅", "/finance-statements"),
        ("표시", "1007", "현금흐름표", "영업/투자/재무 + 직접법 병기", "✅", "/finance-statements"),
        ("표시", "1034", "중간재무보고", "분기 누적·전분기 비교", "✅", "/finance-statements"),
        ("표시", "1108", "영업부문", "진료과별 수익·손익·자산·보고임계 10%", "✅", "/finance-statements"),
        ("공시", "1024", "특수관계자 공시", "거래·잔액 공시 + 적요 자동탐지(보조)", "✅", "/finance-statements"),
        ("공시", "1010", "보고기간후사건", "수정/비수정 구분 공시", "✅", "/finance-statements"),
        ("공시", "1113", "공정가치 측정", "공정가치 서열(Level 1~3) 주석", "✅", "/finance-statements"),
        ("공시", "1008", "회계정책·추정변경·오류", "소급/전진 등록·공시", "✅", "/finance-statements"),
        ("수익", "1115", "고객과의 수익", "미청구수익 발생주의·변동대가(환불부채)", "✅", "/finance-periods"),
    ]

    async def ifrs_conformance(self, session: AsyncSession, *, year: int) -> dict:
        """IFRS 정합 현황 — 기준 자체평가 카탈로그(처리·적용화면·상태) + 당해 실제 처리 활동 병합.

        부합도(conformance)는 카탈로그에서 도출 — 충족(✅)=1, 부분(🟡)=0.5 가중. 하드코딩 금지
        (정직성: 카탈로그가 바뀌면 부합도도 따라가야 화면 '충족 N·부분 M'과 일관).
        """
        act = (await self.ifrs_activity(session, year=year))["activity"]
        items = [{"category": cat, "code": code, "name": name, "treatment": treat,
                  "status": status, "screen": screen, "activity": act.get(code)}
                 for cat, code, name, treat, status, screen in self.CONFORMANCE_CATALOG]
        full = sum(1 for i in items if i["status"] == "✅")
        partial = sum(1 for i in items if i["status"] == "🟡")
        score = round((full + 0.5 * partial) / len(items) * 100) if items else 0
        return {
            "year": str(year),
            "summary": {"conformance": f"~{score}%", "standards": len(items),
                        "measured": full,
                        "note": "자체평가(✅ 충족·🟡 부분 0.5가중 산정). 잔여는 보험계리(완전 DBO)·"
                                "세무조정·외부감사 — SW 외 전문가 영역"},
            "items": items,
            "principles": [
                "모든 결산조정은 결산분개(전표)로 귀결 — 별도 장부 금지·역분개 가능",
                "가정(할인율·대손율·세율·NRV)은 설정값·시점관리, 결산 사전점검 통합",
                "조정순서: 대손→재고→퇴직재측정→이연법인세→이익잉여금",
            ],
        }

    async def ifrs_activity(self, session: AsyncSession, *, year: int) -> dict:
        """당해 IFRS 측정·조정 활동 집계 — 기준별 시그니처 계정의 전표 순변동·건수.

        플로팅 IFRS 마크 동적 표시용. 금액=|차변−대변| 순변동, 건수=전표 수(근사).
        """
        from erp.finance.fiscal import fiscal_range
        start, end = await fiscal_range(session, year)   # 기관 회계연도
        codes = sorted({c for accs in self.STD_ACCOUNTS.values() for c in accs})
        rows = (await session.execute(
            select(VoucherLine.account_code,
                   func.sum(case((VoucherLine.side == "DEBIT", VoucherLine.amount), else_=0)),
                   func.sum(case((VoucherLine.side == "CREDIT", VoucherLine.amount), else_=0)),
                   func.count(func.distinct(VoucherLine.voucher_id)))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code.in_(codes),
                   Voucher.voucher_on >= start, Voucher.voucher_on <= end)
            .group_by(VoucherLine.account_code))).all()
        per = {c: (float(d or 0), float(cr or 0), int(n or 0)) for c, d, cr, n in rows}
        activity: dict[str, dict] = {}
        for std, accs in self.STD_ACCOUNTS.items():
            amt = sum(abs(per.get(a, (0, 0, 0))[0] - per.get(a, (0, 0, 0))[1]) for a in accs)
            cnt = sum(per.get(a, (0, 0, 0))[2] for a in accs)
            if cnt > 0:
                activity[std] = {"amount": round(amt), "count": cnt}
        return {"year": str(year), "activity": activity}

    async def suggest_related_party_txns(self, session: AsyncSession, *, year: int) -> dict:
        """특수관계자 거래 자동 탐지(보조, K-IFRS 1024) — 전표 적요에 등록 특수관계자명이 포함된 건 집계.

        구조적 거래처 연결이 없어 **적요 텍스트 매칭**(휴먼 확인 필수·오탐/미탐 가능). 전표 차변 합계를
        거래금액 추정치로 제시. 확정 등록은 기존 add_rp_txn(수기 확인 후).
        """
        parties = (await session.execute(
            select(RelatedParty).where(RelatedParty.active.is_(True)))).scalars().all()
        if not parties:
            return {"year": str(year), "suggestions": []}
        from erp.finance.fiscal import fiscal_range
        start, end = await fiscal_range(session, year)   # 기관 회계연도
        debit = (select(VoucherLine.voucher_id.label("vid"),
                        func.sum(case((VoucherLine.side == "DEBIT", VoucherLine.amount), else_=0))
                        .label("amt"))
                 .group_by(VoucherLine.voucher_id).subquery())
        rows = (await session.execute(
            select(Voucher.id, Voucher.memo, debit.c.amt)
            .join(debit, debit.c.vid == Voucher.id)
            .where(Voucher.voucher_on >= start, Voucher.voucher_on <= end,
                   Voucher.memo.isnot(None)))).all()
        agg: dict[int, dict] = {p.id: {"party_id": p.id, "party": p.name, "relation": p.relation,
                                       "count": 0, "amount": 0.0} for p in parties}
        for _vid, memo, amt in rows:
            for p in parties:
                if p.name and p.name in (memo or ""):
                    agg[p.id]["count"] += 1
                    agg[p.id]["amount"] += float(amt or 0)
        suggestions = [a for a in agg.values() if a["count"] > 0]
        suggestions.sort(key=lambda a: -a["amount"])
        return {"year": str(year), "suggestions": suggestions,
                "note": "전표 적요 이름매칭 — 휴먼 확인 후 거래 등록(오탐 가능)"}

    async def registry(self, session: AsyncSession, *, year: int) -> dict:
        """연도 등록 공시 묶음 — 특수관계자 거래·우발·후속사건."""
        y = str(year)
        parties = {p.id: p for p in (await session.execute(
            select(RelatedParty))).scalars()}
        txns = (await session.execute(select(RelatedPartyTxn).where(
            RelatedPartyTxn.period == y))).scalars().all()
        cont = (await session.execute(select(Contingency).where(
            Contingency.period == y))).scalars().all()
        subs = (await session.execute(select(SubsequentEvent).where(
            SubsequentEvent.period == y))).scalars().all()
        chgs = (await session.execute(select(AccountingChange).where(
            AccountingChange.period == y))).scalars().all()
        return {
            "related_party_txns": [
                {"party": parties[t.party_id].name if t.party_id in parties else "-",
                 "relation": parties[t.party_id].relation if t.party_id in parties else "-",
                 "txn_type": t.txn_type, "amount": float(t.amount),
                 "balance": float(t.balance), "note": t.note} for t in txns],
            "contingencies": [
                {"id": c.id, "kind": c.kind, "description": c.description,
                 "estimate": float(c.estimate) if c.estimate is not None else None,
                 "likelihood": c.likelihood, "status": c.status} for c in cont],
            "subsequent_events": [
                {"id": e.id, "event_date": str(e.event_date), "description": e.description,
                 "adjusting": e.adjusting, "reflected": e.reflected} for e in subs],
            "accounting_changes": [
                {"id": c.id, "change_type": c.change_type, "standard": c.standard,
                 "description": c.description, "treatment": c.treatment,
                 "effect_amount": float(c.effect_amount) if c.effect_amount is not None else None}
                for c in chgs],
        }

    async def add_accounting_change(self, session: AsyncSession, *, period: str, change_type: str,
                                    description: str, treatment: str = "PROSPECTIVE",
                                    standard: str | None = None,
                                    effect_amount: float | None = None) -> dict:
        c = AccountingChange(period=period, change_type=change_type, description=description,
                             treatment=treatment, standard=standard, effect_amount=effect_amount)
        session.add(c)
        await session.flush()
        return {"id": c.id, "change_type": change_type, "treatment": treatment}

    async def add_related_party(self, session: AsyncSession, *, name: str, relation: str,
                                note: str | None = None) -> dict:
        p = RelatedParty(name=name, relation=relation, note=note)
        session.add(p)
        await session.flush()
        return {"id": p.id, "name": p.name, "relation": p.relation}

    async def add_rp_txn(self, session: AsyncSession, *, party_id: int, period: str,
                         txn_type: str, amount: float, balance: float = 0,
                         note: str | None = None) -> dict:
        t = RelatedPartyTxn(party_id=party_id, period=period, txn_type=txn_type,
                            amount=amount, balance=balance, note=note)
        session.add(t)
        await session.flush()
        return {"id": t.id}

    async def add_contingency(self, session: AsyncSession, *, period: str, kind: str,
                              description: str, estimate: float | None = None,
                              likelihood: str = "중간") -> dict:
        c = Contingency(period=period, kind=kind, description=description,
                        estimate=estimate, likelihood=likelihood)
        session.add(c)
        await session.flush()
        return {"id": c.id}

    async def recognize_provision(self, session: AsyncSession, *, contingency_id: int,
                                  on_date: date, user_id: int | None = None) -> dict:
        """우발부채 → 충당부채 전환(K-IFRS 1037) — 확률 높음·추정가능 시 충당부채 인식.

        `차) 충당부채전입액 5680 / 대) 기타충당부채 2130`(추정액). 멱등(우발항목). status→PROVISIONED.
        우발자산·추정 불가는 인식 불가(공시만).
        """
        from erp.finance.service import VoucherLineDTO, VoucherService
        c = await session.get(Contingency, contingency_id)
        if c is None:
            raise ValidationError("존재하지 않는 우발항목입니다")
        if c.kind != "LIABILITY":
            raise ValidationError("우발부채(LIABILITY)만 충당부채로 전환할 수 있습니다")
        # 멱등/이미처리 가드 — 추정액 검증보다 먼저(이미 인식 건은 추정액 변경과 무관하게 skip)
        sref = f"contingency-provision:{contingency_id}"
        if c.status == "PROVISIONED":
            return {"id": contingency_id, "skipped": "이미 충당부채 인식"}
        if await session.scalar(select(Voucher.id).where(
                Voucher.source_module == "disclosure", Voucher.source_ref == sref)):
            c.status = "PROVISIONED"
            return {"id": contingency_id, "skipped": "이미 전표 존재"}
        if c.status != "OPEN":
            raise ValidationError("OPEN 상태만 전환할 수 있습니다")
        amount = round(float(c.estimate or 0))
        if amount <= 0:
            raise ValidationError("신뢰성 있는 추정액이 있어야 충당부채로 인식할 수 있습니다")
        v = await VoucherService().create(
            session, source_module="disclosure", source_ref=sref, voucher_on=on_date,
            created_by=user_id, memo=f"우발부채 충당부채 전환: {c.description[:60]}",
            lines=[VoucherLineDTO(ACC_PROVISION_EXP, "DEBIT", amount),
                   VoucherLineDTO(ACC_PROVISION_OTHER, "CREDIT", amount)])
        c.status = "PROVISIONED"
        return {"id": contingency_id, "provision": amount, "voucher_id": v.id}

    async def add_subsequent_event(self, session: AsyncSession, *, period: str, event_date: date,
                                   description: str, adjusting: bool = False,
                                   reflected: bool = False) -> dict:
        e = SubsequentEvent(period=period, event_date=event_date, description=description,
                            adjusting=adjusting, reflected=reflected)
        session.add(e)
        await session.flush()
        return {"id": e.id}

    async def save(self, session: AsyncSession, *, year: int, note_key: str, title: str,
                   payload: dict, actor: int | None = None) -> dict:
        row = (await session.execute(select(DisclosureNote).where(
            DisclosureNote.period == str(year),
            DisclosureNote.note_key == note_key))).scalar_one_or_none()
        if row is None:
            row = DisclosureNote(period=str(year), note_key=note_key, title=title,
                                 payload=payload, updated_by=actor)
            session.add(row)
        else:
            row.title, row.payload, row.updated_by = title, payload, actor
        await session.flush()
        return {"note_key": note_key, "title": title, "payload": payload}
