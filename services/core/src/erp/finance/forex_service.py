"""외화 기말환산 (K-IFRS 1021) — 외화 화폐성 항목을 기말환율로 환산, 환산차손익 인식.

외화계좌(corporate_accounts)의 외화 잔액×기말환율 = 환산 KRW. 해당 GL 장부가(KRW)와 차이 = 환산차.
자산 가치 상승 → GL 증가/외화환산이익(4650), 하락 → 외화환산손실(5670)/GL 감소. 멱등(계좌·기간).
미실현 환산손익(외환차익/차손 4630/5660=실현과 구분). 환율은 입력(자동 피드는 후속).
"""
from calendar import monthrange
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import CorporateAccount, Voucher, VoucherLine
from erp.finance.service import VoucherLineDTO, VoucherService

ACC_FX_GAIN = "4650"   # 외화환산이익(미실현)
ACC_FX_LOSS = "5670"   # 외화환산손실(미실현)


def _month_end(yyyymm: str) -> date:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return date(y, m, monthrange(y, m)[1])


class ForexTranslationService:
    def __init__(self) -> None:
        self._vs = VoucherService()

    async def record_movement(self, session: AsyncSession, *, account_id: int, direction: str,
                              fc_amount: float, rate: float, on: date,
                              actor: int | None = None) -> dict:
        """외화 입출금 라우팅(Stage2) — 통화별 계좌 GL로 부킹. 입금: DR 외화예금/CR 현금(1110),
        출금: 반대. KRW=fc_amount×거래환율(취득원가). 이후 기말 환산은 post()가 환산차손익 인식."""
        # 행잠금 — 동시 입출금 경합(seq 충돌) 방지
        acct = await session.scalar(select(CorporateAccount).where(
            CorporateAccount.id == account_id).with_for_update())
        if acct is None:
            raise ValidationError("존재하지 않는 외화계좌입니다")
        if (acct.currency or "KRW") == "KRW":
            raise ValidationError("원화 계좌는 외화 라우팅 대상이 아닙니다")
        if direction not in ("IN", "OUT") or fc_amount <= 0 or rate <= 0:
            raise ValidationError("direction(IN|OUT)·외화금액·환율을 확인하세요")
        krw = round(fc_amount * rate)
        fx, cash = acct.gl_account_code, "1110"
        lines = ([VoucherLineDTO(fx, "DEBIT", krw), VoucherLineDTO(cash, "CREDIT", krw)]
                 if direction == "IN"
                 else [VoucherLineDTO(cash, "DEBIT", krw), VoucherLineDTO(fx, "CREDIT", krw)])
        # 단조 seq source_ref — 같은 날 동일 금액 입출금도 각각 전기(과거 krw 임베드 키는 멱등 충돌로 유실).
        # 계좌ID 뒤 '-'로 구분해 숫자접두 충돌(fx-5-% vs fx-50-...) 방지.
        seq = (await session.scalar(select(func.count()).select_from(Voucher).where(
            Voucher.source_module == "finance",
            Voucher.source_ref.like(f"fx-{account_id}-%")))) + 1
        v = await self._vs.create(
            session, source_module="finance",
            source_ref=f"fx-{account_id}-{seq}",
            voucher_on=on, lines=lines, created_by=actor,
            memo=f"외화 {('입금' if direction == 'IN' else '출금')} {acct.currency} {fc_amount:,.2f}@{rate}")
        return {"account_id": account_id, "currency": acct.currency, "direction": direction,
                "fc_amount": fc_amount, "rate": rate, "krw": krw, "voucher_id": v.id}

    async def _book_value(self, session: AsyncSession, *, as_of: date, code: str) -> float:
        d, cr = (await session.execute(
            select(
                func.coalesce(func.sum(case(
                    (VoucherLine.side == "DEBIT", VoucherLine.amount), else_=0)), 0),
                func.coalesce(func.sum(case(
                    (VoucherLine.side == "CREDIT", VoucherLine.amount), else_=0)), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code == code, Voucher.voucher_on <= as_of,
                   # 미승인·반려 수동전표 배제(statements 규약 — 계산검토 M-1)
                   Voucher.status.in_(("POSTED", "REVERSED"))))).one()
        return float(d) - float(cr)        # 외화예금=자산(차변잔액)

    @staticmethod
    async def _gl_cohabitants(session: AsyncSession, acct: CorporateAccount) -> list:
        """같은 GL을 쓰는 다른 활성 계좌 — 환산 정확도의 전제를 깨는 요인.

        `_book_value`는 **GL 계정코드 전체**를 합산한다. 따라서
        - **다른 통화**가 같은 GL을 쓰면 환산 자체가 성립하지 않는다(통화 혼합 장부가) → 차단.
        - **같은 통화** 여러 계좌가 한 GL을 쓰는 것은 정상 실무지만, 이 API는 계좌 단위로
          `fc_balance`를 받으므로 **그 GL 전체의 외화잔액 합**을 넣어야 맞다 → 경고로 유도.
        ⚠ prod에서 실제로 발현했다: USD 2계좌·JPY 1계좌가 모두 GL 1115를 쓰는 상태에서
        `fx-translate:8:2026-06`이 계좌 #8의 USD 잔액(10,000)을 **GL 1115 전체 장부가**
        (JPY 전기·타 계좌 전기·개설 전기 포함 18,510,500)와 비교해 환산손실 5,510,500을 전기했다.
        해당 계좌들이 전부 TEST 데모라 실계정 피해는 없었으나 코드 경로는 동일하다.
        """
        # ⚠ **비활성 계좌도 센다.** `_book_value`는 전기 이력 전체를 합산하므로, 계좌를 비활성화해도
        # 그 계좌가 남긴 전기는 장부가에 그대로 남는다. is_active로 거르면 "계좌를 끄면 가드가
        # 풀리는" 우회가 생겨, 가드가 막으려던 바로 그 상황(통화 섞인 장부가)이 조용히 돌아온다.
        # 해소는 계좌 비활성화가 아니라 **전기 이관**(역분개 후 전용 GL로 재전기)이다.
        return (await session.execute(
            select(CorporateAccount.id, CorporateAccount.currency).where(
                CorporateAccount.gl_account_code == acct.gl_account_code,
                CorporateAccount.id != acct.id))).all()

    async def preview(self, session: AsyncSession, *, account_id: int, fc_balance: float,
                      rate: float, yyyymm: str) -> dict:
        acct = await session.get(CorporateAccount, account_id)
        if acct is None:
            raise ValueError("존재하지 않는 외화계좌입니다")
        if (acct.currency or "KRW") == "KRW":
            raise ValueError("원화 계좌는 환산 대상이 아닙니다")
        others = await self._gl_cohabitants(session, acct)
        mixed = [(i, c) for i, c in others if (c or "") != acct.currency]
        if mixed:      # 통화 혼입 — 환산차가 조용히 틀린다. 전기하느니 막는다.
            who = ", ".join(f"#{i}({c})" for i, c in mixed)
            raise ValidationError(
                f"GL {acct.gl_account_code}를 다른 통화 계좌가 공유합니다({who}) — 장부가에 여러 통화가 "
                f"섞여 환산차가 부정확해집니다. 통화마다 전용 GL 계정을 지정하세요.")
        until = _month_end(yyyymm)
        translated = round(float(fc_balance) * float(rate))
        book = round(await self._book_value(session, as_of=until, code=acct.gl_account_code))
        out = {"account_id": account_id, "currency": acct.currency, "gl": acct.gl_account_code,
               "fc_balance": float(fc_balance), "rate": float(rate),
               "translated_krw": translated, "book_value": book, "delta": translated - book}
        if others:     # 동일 통화 다계좌 — 장부가가 합산이므로 fc_balance도 합계여야 한다
            out["shared_gl_accounts"] = [i for i, _c in others]
            out["warning"] = (f"GL {acct.gl_account_code}를 같은 통화 계좌 {len(others)}건이 공유합니다 — "
                              f"fc_balance에 해당 GL **전체 외화잔액 합계**를 입력해야 환산차가 맞습니다.")
        return out

    async def post(self, session: AsyncSession, *, account_id: int, fc_balance: float,
                   rate: float, yyyymm: str, actor: int | None = None) -> dict:
        # 2026-10-09 전체 흐름 검토 D·MED-2 — 종전엔 allow_closed=True로 전기해, 사용자가 넣은 환율·
        # 외화잔액만으로 **마감된 기간에 승인 없이 임의 금액**이 들어갔다(마감 SoD·재개 승인 우회).
        # 마감 기간은 재개방(approve 권한·사유·감사)을 거쳐야 환산 전기할 수 있다.
        from erp.finance.models import FiscalPeriod
        if (await session.scalar(select(FiscalPeriod.status).where(
                FiscalPeriod.yyyymm == yyyymm))) == "CLOSED":
            raise ValidationError(
                f"마감된 기간({yyyymm})에는 외화환산을 전기할 수 없습니다 — "
                f"재무 승인권자가 기간을 재개방한 뒤 다시 실행하세요")
        # 멱등 재호출 — 종전엔 create()가 기존 전표를 돌려줘도 posted:True와 **이번 입력으로 새로 계산한
        # delta**를 응답·감사에 실어, 실제로는 아무것도 전기되지 않았는데 전기된 것처럼 보였다.
        base = f"fx-translate:{account_id}:{yyyymm}"
        priors = (await session.scalars(select(Voucher).where(
            Voucher.source_module == "closing-ifrs",
            Voucher.reverses.is_(None),        # 역분개 전표 제외(원본 실행만) — 3차 검토 D
            (Voucher.source_ref == base) | Voucher.source_ref.like(f"{base}:r%")))).all()
        live = next((v for v in priors if v.status != "REVERSED"), None)
        if live is not None:
            return {"posted": False, "voucher_id": live.id,
                    "reason": f"이미 {yyyymm} 환산 전표(#{live.id})가 있습니다 — 정정은 그 전표를 역분개한 뒤 재실행"}
        # 역분개된 이전 실행이 있으면 새 키로 — source_ref UNIQUE 때문에 역분개 후에도 재실행이 막혀
        # 안내한 정정 경로가 실제로는 없었다(2차 검토 D)
        ref = base if not priors else f"{base}:r{len(priors)}"
        p = await self.preview(session, account_id=account_id, fc_balance=fc_balance,
                               rate=rate, yyyymm=yyyymm)
        delta = p["delta"]
        if delta == 0:
            return {"posted": False, "reason": "환산차 없음", **p}
        until = _month_end(yyyymm)
        gl = p["gl"]
        if delta > 0:        # 외화자산 가치↑ → GL 증가 / 외화환산이익
            lines = [VoucherLineDTO(gl, "DEBIT", delta),
                     VoucherLineDTO(ACC_FX_GAIN, "CREDIT", delta)]
        else:                # 가치↓ → 외화환산손실 / GL 감소
            lines = [VoucherLineDTO(ACC_FX_LOSS, "DEBIT", -delta),
                     VoucherLineDTO(gl, "CREDIT", -delta)]
        v = await self._vs.create(
            session, source_module="closing-ifrs", source_ref=ref,
            voucher_on=until, lines=lines, created_by=actor,
            memo=f"외화환산({p['currency']} {rate}, K-IFRS 1021)")
        return {"posted": True, "voucher_id": v.id, **p}
