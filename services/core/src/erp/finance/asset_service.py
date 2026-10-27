"""고정자산·감가상각 (FR-09 확장) — 취득·월감가상각·처분. 모두 전표 생성으로 재무제표 반영.

감가상각: 정액(STRAIGHT)=(취득가−잔존가)/내용연수/12, 정률(DECLINING)=장부가×상각률/12.
전표: 취득 의료장비(차)/미지급(대), 감가 감가상각비(차)/감가상각누계액(대, 자산 차감).
"""
import calendar
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import DepreciationEntry, FixedAsset, Voucher
from erp.finance.service import VoucherLineDTO, VoucherService

ACC_EQUIP = "1710"      # 의료장비(자산)
ACC_ACCUM = "1810"      # 감가상각누계액(자산 차감)
ACC_PAYABLE = "2110"    # 미지급금(부채)
ACC_CASH = "1110"       # 현금성자산
ACC_DEP_EXP = "5210"    # 감가상각비(비용)
ACC_DISPOSAL_GAIN = "4610"   # 유형자산처분이익(수익)
ACC_DISPOSAL_LOSS = "5610"   # 유형자산처분손실(비용)
ACC_IMPAIR_LOSS = "5620"     # 유형자산손상차손(비용)
ACC_IMPAIR_ACCUM = "1815"    # 손상차손누계액(자산 차감)
ACC_IMPAIR_REVERSAL = "4640"  # 유형자산손상차손환입(수익, K-IFRS 1036) — 4630=외환차익(기존)과 구분
ACC_REVAL_SURPLUS = "3400"   # 재평가잉여금(자본/OCI)
ACC_REVAL_LOSS = "5650"      # 재평가손실(비용)


class AssetService:
    def __init__(self, voucher_service: VoucherService | None = None):
        self._vouchers = voucher_service or VoucherService()

    async def register(self, session: AsyncSession, *, asset_no: str, name: str,
                       category: str, acquired_on: date, acquisition_cost: float,
                       useful_life_years: int, salvage_value: float = 0,
                       method: str = "STRAIGHT", department_id: int | None = None,
                       payment_method: str = "PAYABLE",
                       created_by: int | None = None,
                       vendor: str | None = None, due_on: date | None = None,
                       create_payable: bool = False) -> FixedAsset:
        if useful_life_years <= 0:
            raise ValidationError("내용연수는 1년 이상이어야 합니다")
        if acquisition_cost <= salvage_value:
            raise ValidationError("취득가는 잔존가보다 커야 합니다")
        asset = FixedAsset(asset_no=asset_no, name=name, category=category,
                           acquired_on=acquired_on, acquisition_cost=acquisition_cost,
                           salvage_value=salvage_value, useful_life_years=useful_life_years,
                           method=method, department_id=department_id)
        session.add(asset)
        await session.flush()
        # 결제수단: 현금(CASH) 또는 미지급(PAYABLE) — 의료장비(차) / 현금|미지급(대)
        credit_acc = ACC_CASH if payment_method == "CASH" else ACC_PAYABLE
        cc = await self._cost_center_for(session, asset)
        await self._vouchers.create(
            session, source_module="asset", source_ref=f"asset-acq-{asset_no}",
            voucher_on=acquired_on, memo=f"고정자산 취득: {name}", created_by=created_by,
            lines=self._tag_cc([VoucherLineDTO(ACC_EQUIP, "DEBIT", acquisition_cost),
                                VoucherLineDTO(credit_acc, "CREDIT", acquisition_cost)], cc))
        # 2026-10-09 전체 흐름 검토 2차 E: Payable 행 생성은 **옵트인**(create_payable). HIS
        # equipment.acquired(inbound_consumer)도 PAYABLE로 이 함수를 부르는데, 이관된 수년 전 자산까지
        # OUTSTANDING 채무(+30일 연체)로 생겨 AP aging·자금예측을 오염시키고 지급결의로 지급될 수 있었다.
        # 수기 취득 엔드포인트만 True를 넘긴다 — HIS 경로는 종전처럼 GL(DR 1710/CR 2110)만.
        if credit_acc == ACC_PAYABLE and create_payable:
            # 2026-10-09 전체 흐름 검토 E: 미지급 취득은 GL에 CR 2110만 찍고 보조원장(Payable) 행이
            # 없어 AP 화면·지급결의로 지급할 수 없었다(지급하려면 수기 Payable=비용 이중계상 위험).
            # 전표는 위에서 이미 DR 1710/CR 2110으로 끝났으므로 record_payable(비용 전표 동반)이 아니라
            # Payable 행만 같은 금액으로 만든다 — GL 2110 잔액 = 보조원장 미지급 잔액.
            from datetime import timedelta

            from erp.finance.models import Payable
            session.add(Payable(
                party=(vendor or f"고정자산 취득 {name}")[:120],
                description=f"고정자산 취득 {asset_no}"[:200],
                amount=round(float(acquisition_cost)), paid_amount=0,
                incurred_on=acquired_on, due_on=due_on or acquired_on + timedelta(days=30),
                status="OUTSTANDING", source_module="asset",
                source_ref=f"asset-acq-{asset_no}"[:64]))
            await session.flush()
        return asset

    def monthly_amount(self, asset: FixedAsset, current_accum: float,
                       prior_periods: int = 0) -> float:
        """월 감가상각액 — 잔존가 미만 상각 방지. 정률은 경과 기간(prior_periods)으로
        결정론적 계산(누계 달러 상태 비의존 → 월 누락·재실행 순서 무관)."""
        depreciable = float(asset.acquisition_cost) - float(asset.salvage_value)
        remaining = depreciable - float(current_accum)
        if remaining <= 0:
            return 0.0
        if asset.method == "STRAIGHT":
            monthly = depreciable / (asset.useful_life_years * 12)
        else:  # DECLINING — 정률(잔존가율 기반), 경과 개월수로 장부가 결정론 산출
            cost = float(asset.acquisition_cost)
            salv = max(float(asset.salvage_value), 1.0)
            monthly_rate = (1 - (salv / cost) ** (1 / asset.useful_life_years)) / 12
            book_at_start = cost * (1 - monthly_rate) ** prior_periods
            monthly = book_at_start * monthly_rate
        return float(round(min(monthly, remaining)))

    @staticmethod
    def _tag_cc(lines: list[VoucherLineDTO], cc: str | None) -> list[VoucherLineDTO]:
        """자산 생애주기 전기 라인에 cost_center(소유부서) 일괄 스탬프(G9·명시값 우선)."""
        if cc:
            for ln in lines:
                if ln.cost_center is None:
                    ln.cost_center = cc
        return lines

    async def _cost_center_for(self, session: AsyncSession, asset: FixedAsset,
                               cache: dict | None = None) -> str | None:
        """감가 전기의 cost_center(G9) — 자산 소유부서 코드(책임센터). 배치는 cache로 조회 최소화."""
        if asset.department_id is None:
            return None
        if cache is not None and asset.department_id in cache:
            return cache[asset.department_id]
        from erp.common.models import Department
        code = await session.scalar(
            select(Department.code).where(Department.id == asset.department_id))
        cc = code or f"DEPT-{asset.department_id}"
        if cache is not None:
            cache[asset.department_id] = cc
        return cc

    async def _post_month(self, session: AsyncSession, asset: FixedAsset, period: str,
                          created_by: int | None = None, cc_cache: dict | None = None) -> float:
        """자산 1건 period(yyyymm) 감가 전기(멱등·잔존가 floor). 반환: 전기액(0=중복/상각완료).

        run_depreciation·_depreciate_through 공용 — 월감가 로직 단일화(누계·전표·엔트리 일관)."""
        dup = await session.scalar(
            select(func.count()).select_from(DepreciationEntry)
            .where(DepreciationEntry.asset_id == asset.id, DepreciationEntry.period == period))
        if dup:
            return 0.0
        prior = await session.scalar(
            select(func.count()).select_from(DepreciationEntry)
            .where(DepreciationEntry.asset_id == asset.id))
        amount = self.monthly_amount(asset, asset.accumulated_depreciation, prior_periods=prior)
        if amount <= 0:
            return 0.0
        asset.accumulated_depreciation = float(asset.accumulated_depreciation) + amount
        book_after = float(asset.acquisition_cost) - float(asset.accumulated_depreciation)
        year, month = int(period[:4]), int(period[5:7])
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        ref = f"depr-{asset.asset_no}-{period}"
        cc = await self._cost_center_for(session, asset, cc_cache)   # G9 자동채움 — 소유부서 cost center
        await self._vouchers.create(
            session, source_module="asset", source_ref=ref, voucher_on=last_day,
            memo=f"감가상각 {period}: {asset.name}", created_by=created_by,
            lines=[VoucherLineDTO(ACC_DEP_EXP, "DEBIT", amount, cost_center=cc),
                   VoucherLineDTO(ACC_ACCUM, "CREDIT", amount, cost_center=cc)])
        session.add(DepreciationEntry(asset_id=asset.id, period=period, amount=amount,
                                      book_value_after=book_after, voucher_ref=ref))
        return amount

    async def _depreciate_through(self, session: AsyncSession, asset: FixedAsset,
                                  through: date, created_by: int | None = None) -> int:
        """이벤트(손상·재평가·처분) 전 감가를 through(이벤트일)까지 현행화 — 타임라인 정합.

        손상·재평가·처분은 **현행 장부가** 기준으로 측정/제거돼야 한다(IAS 16.55: 처분일까지 상각).
        감가가 밀린 상태에서 손상을 측정하면 과대 손상, 이후 처분 시 밀린 감가를 소급 채우면 가치
        감소가 이중계상된다(과거 되돌림 원인). 각 이벤트 직전 이 헬퍼로 감가를 현행화하면 모든
        측정이 현행 장부가 기준이라 이중계상이 원천 차단된다.

        **스케줄(DepreciationEntry)이 있는 자산만** 최종 기입월 다음 달부터 through 월까지 미기입
        월을 멱등 전기한다. 무기입(수동 입력·이관·초기 적재 자산)은 stored accumulated_depreciation을
        SoT로 신뢰해 손대지 않는다(월배치가 유일 전진자인 현 모델·기존 동작 보존). 반환: 추가 전기 월수."""
        if asset.status == "DISPOSED":
            return 0
        last = await session.scalar(
            select(func.max(DepreciationEntry.period))
            .where(DepreciationEntry.asset_id == asset.id))
        if last is None:
            return 0                     # 스케줄 없음 — stored 누계를 SoT로 보존
        y, m = int(last[:4]), int(last[5:7])
        posted = 0
        while True:
            m += 1
            if m > 12:
                y, m = y + 1, 1
            # 월배치와 동일한 월말 인식 — 월말(last_day)이 이벤트일 이하인 완료 월만 전기.
            # 처분/측정일이 월 초면 그 달은 미완료라 제외(예: 2/1 처분 → 2월 감가 없음).
            if date(y, m, calendar.monthrange(y, m)[1]) > through:
                break
            if await self._post_month(session, asset, f"{y}-{m:02d}", created_by) > 0:
                posted += 1
        return posted

    async def run_depreciation(self, session: AsyncSession, *, period: str,
                               created_by: int | None = None) -> int:
        """period(yyyymm) 미상각 IN_USE 자산 월 감가상각 + 전표. 자산×기간 멱등. 반환: 처리 건수."""
        year, month = int(period[:4]), int(period[5:7])
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        assets = (await session.execute(select(FixedAsset).where(
            FixedAsset.status == "IN_USE",
            FixedAsset.acquired_on <= last_day))).scalars().all()
        done = 0
        cc_cache: dict = {}     # G9 — 부서코드 조회 캐시(배치 조회 최소화)
        for a in assets:
            if await self._post_month(session, a, period, created_by, cc_cache) > 0:
                done += 1
        return done

    async def dispose(self, session: AsyncSession, *, asset_id: int, disposed_on: date,
                      disposal_amount: float = 0, created_by: int | None = None) -> dict:
        """처분 — 자산제거·감가누계 상계·처분손익 전표. 반환: 처분손익 포함 결과."""
        asset = await session.get(FixedAsset, asset_id)
        if asset is None:
            raise ValidationError("존재하지 않는 자산입니다")
        if asset.status == "DISPOSED":
            raise ValidationError("이미 처분된 자산입니다")
        if disposal_amount < 0:
            raise ValidationError("처분금액은 0 이상이어야 합니다")
        # 처분일까지 감가 현행화(IAS 16.55) — 밀린 감가를 채워 처분손익을 현행 장부가로 정확히 산출.
        await self._depreciate_through(session, asset, disposed_on, created_by)
        # 구성요소를 먼저 정수화 후 book_value·gain 도출 — 각 라인 독립 반올림 시 차대 ±1원
        # 불일치로 처분 전표가 균형검증에 막혀 처분 자체가 실패하던 결함 방지(intangible.dispose와 동일).
        cost = round(float(asset.acquisition_cost))
        accum = round(float(asset.accumulated_depreciation))
        impair = round(float(asset.accumulated_impairment or 0))
        reval = round(float(asset.accumulated_revaluation or 0))
        carrying = cost + reval                # 1710 잔액(재평가 반영)
        book_value = carrying - accum - impair
        proceeds = round(float(disposal_amount))
        gain = proceeds - book_value           # +이익 / −손실
        asset.status = "DISPOSED"
        asset.disposed_on = disposed_on

        # 자산제거(의료장비 대변, 재평가 포함 carrying) + 감가/손상누계 상계(차변) + 처분손익
        lines = [VoucherLineDTO(ACC_EQUIP, "CREDIT", carrying)]
        if reval > 0:                          # 재평가잉여금 → 이익잉여금 대체(처분 시 실현, 균형쌍)
            lines.append(VoucherLineDTO(ACC_REVAL_SURPLUS, "DEBIT", reval))
            lines.append(VoucherLineDTO("3300", "CREDIT", reval))
        if accum > 0:
            lines.append(VoucherLineDTO(ACC_ACCUM, "DEBIT", accum))
        if impair > 0:
            lines.append(VoucherLineDTO(ACC_IMPAIR_ACCUM, "DEBIT", impair))
        if proceeds > 0:
            lines.append(VoucherLineDTO(ACC_CASH, "DEBIT", proceeds))
        if gain > 0:
            lines.append(VoucherLineDTO(ACC_DISPOSAL_GAIN, "CREDIT", gain))
        elif gain < 0:
            lines.append(VoucherLineDTO(ACC_DISPOSAL_LOSS, "DEBIT", -gain))
        await self._vouchers.create(
            session, source_module="asset", source_ref=f"asset-disp-{asset.asset_no}",
            voucher_on=disposed_on, memo=f"고정자산 처분: {asset.name}", created_by=created_by,
            lines=self._tag_cc(lines, await self._cost_center_for(session, asset)))
        return {"id": asset.id, "status": asset.status, "book_value": book_value,
                "proceeds": proceeds, "gain_loss": round(gain)}

    async def impair(self, session: AsyncSession, *, asset_id: int, recoverable_amount: float,
                     on_date: date, created_by: int | None = None) -> dict:
        """손상차손 — 회수가능액 < 장부가액 시 차액을 손상차손/손상누계 전기. 멱등(자산·일자)."""
        asset = await session.get(FixedAsset, asset_id)
        if asset is None:
            raise ValidationError("존재하지 않는 자산입니다")
        if asset.status == "DISPOSED":
            raise ValidationError("처분된 자산은 손상 인식 불가")
        sref = f"asset-impair-{asset.asset_no}-{on_date}"
        if await self._posted(session, sref):   # 멱등 선검사 — 누계 이중증가 방지(전표 멱등과 대칭)
            return {"id": asset.id, "skipped": "이미 손상 인식(동일 자산·일자)"}
        # 손상 측정 전 감가 현행화 — 밀린 감가 위에서 손상을 측정하면 과대 손상(타임라인 정합).
        await self._depreciate_through(session, asset, on_date, created_by)
        reval = round(float(asset.accumulated_revaluation or 0))
        book = (float(asset.acquisition_cost) + reval - float(asset.accumulated_depreciation)
                - float(asset.accumulated_impairment or 0))
        loss = round(book - float(recoverable_amount))
        if loss <= 0:
            raise ValidationError(f"회수가능액이 장부가액({round(book):,}) 이상 — 손상 없음")
        asset.accumulated_impairment = float(asset.accumulated_impairment or 0) + loss
        # 재평가모형: 손상은 재평가잉여금(OCI)에서 먼저 차감, 초과분만 손상차손(P/L) — K-IFRS 1016/1036
        from_surplus = min(max(reval, 0), loss)
        to_pl = loss - from_surplus
        debit_lines = []
        if from_surplus:
            debit_lines.append(VoucherLineDTO(ACC_REVAL_SURPLUS, "DEBIT", from_surplus))
            asset.accumulated_revaluation = reval - from_surplus
        if to_pl:
            debit_lines.append(VoucherLineDTO(ACC_IMPAIR_LOSS, "DEBIT", to_pl))
        await self._vouchers.create(
            session, source_module="asset", source_ref=sref,
            voucher_on=on_date, memo=f"고정자산 손상차손: {asset.name}", created_by=created_by,
            lines=self._tag_cc([*debit_lines, VoucherLineDTO(ACC_IMPAIR_ACCUM, "CREDIT", loss)],
                               await self._cost_center_for(session, asset)))
        return {"id": asset.id, "book_before": round(book), "recoverable": round(recoverable_amount),
                "impairment_loss": loss, "oci_portion": from_surplus, "pl_portion": to_pl,
                "book_after": round(book - loss)}

    async def reverse_impairment(self, session: AsyncSession, *, asset_id: int,
                                 recoverable_amount: float, on_date: date,
                                 created_by: int | None = None) -> dict:
        """손상차손 환입(K-IFRS 1036) — 회수가능액 회복 시 손상누계 한도 내 환입(원가모형).

        환입 = min(회수가능액 − 장부가, 손상누계). 손상을 인식하지 않았을 때의 장부가(원가−감가) 초과 불가.
        환입액은 당기손익(손상차손환입 4640, 원가모형) 또는 OCI(재평가모형). 멱등(자산·일자).
        """
        asset = await session.get(FixedAsset, asset_id)
        if asset is None:
            raise ValidationError("존재하지 않는 자산입니다")
        if asset.status == "DISPOSED":
            raise ValidationError("처분된 자산은 손상 환입 불가")
        accum_impair = round(float(asset.accumulated_impairment or 0))
        if accum_impair <= 0:
            raise ValidationError("인식된 손상이 없어 환입할 수 없습니다")
        sref = f"asset-impair-reverse-{asset.asset_no}-{on_date}"
        if await self._posted(session, sref):
            return {"id": asset.id, "skipped": "이미 손상 환입(동일 자산·일자)"}
        # 환입 한도(손상없을 때 장부가) 판정 전 감가 현행화 — 현행 장부가 기준(타임라인 정합).
        await self._depreciate_through(session, asset, on_date, created_by)
        book = (float(asset.acquisition_cost) + float(asset.accumulated_revaluation or 0)
                - float(asset.accumulated_depreciation) - accum_impair)
        # 한도: 손상없을 때 장부가(원가−감가) = book + accum_impair, 회수가능액까지만
        reversal = min(round(float(recoverable_amount)) - round(book), accum_impair)
        if reversal <= 0:
            raise ValidationError(f"회수가능액이 장부가액({round(book):,}) 이하 — 환입 없음")
        asset.accumulated_impairment = accum_impair - reversal
        # 재평가모형 자산: 환입을 재평가잉여금(OCI)으로, 원가모형은 당기손익(환입 수익) — K-IFRS 1016/1036
        on_revaluation = asset.fair_value_level is not None
        credit_acct = ACC_REVAL_SURPLUS if on_revaluation else ACC_IMPAIR_REVERSAL
        if on_revaluation:
            asset.accumulated_revaluation = round(float(asset.accumulated_revaluation or 0)) + reversal
        await self._vouchers.create(
            session, source_module="asset", source_ref=sref,
            voucher_on=on_date, memo=f"고정자산 손상차손환입: {asset.name}", created_by=created_by,
            lines=self._tag_cc([VoucherLineDTO(ACC_IMPAIR_ACCUM, "DEBIT", reversal),
                                VoucherLineDTO(credit_acct, "CREDIT", reversal)],
                               await self._cost_center_for(session, asset)))
        return {"id": asset.id, "book_before": round(book), "recoverable": round(recoverable_amount),
                "reversal": reversal, "to_oci": on_revaluation, "book_after": round(book + reversal)}

    async def _posted(self, session: AsyncSession, source_ref: str) -> bool:
        """동일 자산 전표(source_ref) 기전기 여부 — 멱등 선검사용."""
        return (await session.scalar(select(Voucher.id).where(
            Voucher.source_module == "asset", Voucher.source_ref == source_ref))) is not None

    async def revalue(self, session: AsyncSession, *, asset_id: int, fair_value: float,
                      on_date: date, fair_value_level: str = "3",
                      created_by: int | None = None) -> dict:
        """재평가 — 공정가치-장부가액 차이. 증가=자산↑/재평가잉여금, 감소=잉여금차감 후 손실."""
        asset = await session.get(FixedAsset, asset_id)
        if asset is None:
            raise ValidationError("존재하지 않는 자산입니다")
        if asset.status == "DISPOSED":
            raise ValidationError("처분된 자산은 재평가 불가")
        sref = f"asset-reval-{asset.asset_no}-{on_date}"
        if await self._posted(session, sref):   # 멱등 선검사 — 잉여금 이중반영 방지
            return {"id": asset.id, "skipped": "이미 재평가(동일 자산·일자)"}
        # 재평가 차이(공정가치−장부가) 측정 전 감가 현행화 — 현행 장부가 기준(타임라인 정합).
        await self._depreciate_through(session, asset, on_date, created_by)
        reval = float(asset.accumulated_revaluation or 0)
        book = (float(asset.acquisition_cost) + reval - float(asset.accumulated_depreciation)
                - float(asset.accumulated_impairment or 0))
        delta = round(float(fair_value) - book)
        if delta == 0:
            raise ValidationError(f"공정가치가 장부가액({round(book):,})과 동일 — 변동 없음")
        if delta > 0:                        # 평가증 → 자산↑ / 재평가잉여금(OCI)
            lines = [VoucherLineDTO(ACC_EQUIP, "DEBIT", delta),
                     VoucherLineDTO(ACC_REVAL_SURPLUS, "CREDIT", delta)]
        else:                                # 평가감 → 기존 잉여금 차감 후 초과분 손실
            dec = -delta
            from_surplus = min(max(reval, 0), dec)
            to_loss = dec - from_surplus
            lines = [VoucherLineDTO(ACC_EQUIP, "CREDIT", dec)]
            if from_surplus:
                lines.append(VoucherLineDTO(ACC_REVAL_SURPLUS, "DEBIT", from_surplus))
            if to_loss:
                lines.append(VoucherLineDTO(ACC_REVAL_LOSS, "DEBIT", to_loss))
        asset.accumulated_revaluation = reval + delta
        if fair_value_level in ("1", "2", "3"):
            asset.fair_value_level = fair_value_level
        await self._vouchers.create(
            session, source_module="asset", source_ref=sref,
            voucher_on=on_date, memo=f"고정자산 재평가: {asset.name}", created_by=created_by,
            lines=self._tag_cc(lines, await self._cost_center_for(session, asset)))
        return {"id": asset.id, "book_before": round(book), "fair_value": round(float(fair_value)),
                "delta": delta, "book_after": round(book + delta)}

    async def list_assets(self, session: AsyncSession) -> list[dict]:
        rows = (await session.execute(
            select(FixedAsset).order_by(FixedAsset.id))).scalars().all()
        return [{"id": a.id, "asset_no": a.asset_no, "name": a.name, "category": a.category,
                 "acquired_on": a.acquired_on, "acquisition_cost": float(a.acquisition_cost),
                 "accumulated_depreciation": float(a.accumulated_depreciation),
                 "accumulated_impairment": float(a.accumulated_impairment or 0),
                 "accumulated_revaluation": float(a.accumulated_revaluation or 0),
                 "book_value": float(a.acquisition_cost) + float(a.accumulated_revaluation or 0)
                 - float(a.accumulated_depreciation) - float(a.accumulated_impairment or 0),
                 "useful_life_years": a.useful_life_years, "method": a.method,
                 "status": a.status} for a in rows]
