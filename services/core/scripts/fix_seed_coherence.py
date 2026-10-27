"""시드 정합 조정 — 개발 환경 데모 데이터의 원장·미러 불일치 해소.

배경(v1.271.0에서 발견):
  법인계좌 3개(USD #6·#8, JPY #7)가 모두 GL `1115`를 공유하고 있었다. `_book_value`는 GL 계정코드
  전체를 합산하므로 통화가 섞이면 환산차가 무의미해지고, 실제로 `fx-translate:8:2026-06`이
  계좌 #8의 USD 잔액을 JPY·타 계좌 전기까지 포함한 장부가와 비교해 **환산손실 5,510,500을
  오전기**했다(voucher 205). v1.271.0이 추가 오전기는 막았으나 **이미 쌓인 시드는 그대로**라,
  개발 중 환산 화면이 상시 차단·왜곡 상태로 남는다.

이 스크립트가 하는 일(전부 멱등):
  1. `1116 외화예금(JPY)` 계정 마스터 생성
  2. JPY 법인계좌 #7의 GL을 1116으로 이관 → 통화 혼입 해소(USD #6·#8은 동일 통화라 1115 공유 유지)
  3. JPY 데모 전기(`voucher:purchase:TEST-FX-J1`)를 역분개 후 1116으로 재전기
     ⚠ 원 전기월(2026-03)은 **마감(CLOSED)** 이라 그 일자로는 전기할 수 없다. 역분개·재전기 모두
       현재 개방기간(오늘) 일자로 넣는다 — 마감 기간을 건드리지 않는 정석 정정 경로다.
  4. 오전기 환산손실(voucher 205) 역분개
  5. AR 미러만 있고 GL 전기가 없던 demo-ifrs 채권 3건 백필

배경(AR):
  `finance.receivables`의 demo-ifrs 3건(33,000,000)이 **미러만 시드되고 GL 전기가 없었다.**
  그런데 ECL 충당금(`allowance:AR:2026-06`)은 미러 기준 35,000,000으로 계산돼 8,540,000을
  전기했다. 결과적으로 재무상태표에 `미수금 2,000,000 − 대손충당금 8,540,000 = 순채권 -6,540,000`
  이라는 음수가 찍혔고, 채권관리 화면(35,000,000)과 재무제표(2,000,000)도 어긋났다.
  → 누락 GL을 백필한다. 상대계정은 `3300 이익잉여금` — 과거 기간에 발생한 채권이므로 당월
    손익(4110)으로 잡으면 이번 달 수익이 부풀려진다.

원장은 불변(트리거 `voucher_immutable`)이라 **삭제·계정 변경이 아니라 역분개**로만 정정한다.
전표 자체는 보존되고 반대 분개가 추가되므로 감사추적이 남는다.

실행(컨테이너에 scripts/ 미포함):
  docker cp .../fix_seed_fx_coherence.py infra-core-1:/tmp/
  docker exec -w /app -e PYTHONPATH=/app/src infra-core-1 python /tmp/fix_seed_fx_coherence.py
"""
import asyncio
from datetime import date

from sqlalchemy import select

from erp.db import session_factory
from erp.finance.models import Account, CorporateAccount, Voucher
from erp.finance.service import VoucherLineDTO, VoucherService

GL_JPY = "1116"
ACC_AR = "1130"          # 미수금
ACC_RETAINED = "3300"    # 이익잉여금 — 과거 기간 발생분의 상대계정
REASON = "시드 정합 조정 — 외화 GL 통화 혼입 해소(v1.271.0 후속)"


async def ensure_jpy_account(s) -> None:
    if await s.scalar(select(Account).where(Account.code == GL_JPY)):
        print(f"  · {GL_JPY} 계정 이미 존재 — 건너뜀")
        return
    base = await s.scalar(select(Account).where(Account.code == "1115"))
    s.add(Account(code=GL_JPY, name="외화예금(JPY)", kind="ASSET",
                  is_current=True, parent_id=base.parent_id if base else None))
    await s.flush()
    print(f"  · {GL_JPY} 외화예금(JPY) 계정 생성")


async def move_jpy_corp_account(s) -> CorporateAccount | None:
    acct = await s.scalar(select(CorporateAccount).where(CorporateAccount.currency == "JPY"))
    if acct is None:
        print("  · JPY 법인계좌 없음 — 건너뜀")
        return None
    if acct.gl_account_code == GL_JPY:
        print(f"  · JPY 계좌 #{acct.id} 이미 {GL_JPY} — 건너뜀")
        return acct
    print(f"  · JPY 계좌 #{acct.id} GL {acct.gl_account_code} → {GL_JPY}")
    acct.gl_account_code = GL_JPY
    await s.flush()
    return acct


async def reverse_once(s, vs, *, source_ref: str, label: str) -> Voucher | None:
    """해당 source_ref 전표를 1회만 역분개(이미 REVERSED면 건너뜀)."""
    v = await s.scalar(select(Voucher).where(Voucher.source_ref == source_ref))
    if v is None:
        print(f"  · {label}: 전표 없음 — 건너뜀")
        return None
    if v.status == "REVERSED":
        print(f"  · {label}: 이미 역분개됨(#{v.id}) — 건너뜀")
        return None
    rv = await vs.reverse(s, voucher_id=v.id, reason=REASON)
    print(f"  · {label}: #{v.id} 역분개 → #{rv.id}")
    return v


async def rebook_jpy(s, vs) -> None:
    """JPY 데모 전기를 1115 → 1116으로 재배치.

    ⚠ 재전기 라인은 **원 전표에서 유도**한다. 처음에 상대계정을 1110(현금)으로 하드코딩했다가
    원 전표의 2140(미지급)과 달라져 부채가 450,000 줄고 현금이 줄어드는 왜곡을 만들었다
    (차대는 맞아 항등식 검증을 통과했다 — 균형이 맞는다고 의미가 맞는 것은 아니다).
    """
    from erp.finance.models import VoucherLine

    orig = await s.scalar(select(Voucher).where(
        Voucher.source_ref == "voucher:purchase:TEST-FX-J1"))
    if orig is None:
        print("  · 원 전표 없음 — 건너뜀")
        return

    # 잘못된 1차 재전기(상대계정 1110) 정정
    bad = await s.scalar(select(Voucher).where(Voucher.source_ref == "TEST-FX-J1-REBOOK"))
    if bad is not None and bad.status != "REVERSED":
        bad_lines = (await s.execute(select(VoucherLine).where(
            VoucherLine.voucher_id == bad.id))).scalars().all()
        if any(line.account_code == "1110" for line in bad_lines):
            rv = await vs.reverse(s, voucher_id=bad.id,
                                  reason="재전기 상대계정 오류(1110→2140) 정정")
            print(f"  · 잘못된 재전기 #{bad.id} 역분개 → #{rv.id}")

    if orig.status != "REVERSED":
        rv = await vs.reverse(s, voucher_id=orig.id, reason=REASON)
        print(f"  · 원 전기 #{orig.id} 역분개 → #{rv.id}")

    ref = "TEST-FX-J1-REBOOK-V2"
    if await s.scalar(select(Voucher).where(Voucher.source_ref == ref)):
        print("  · 재전기(V2) 이미 존재 — 건너뜀")
        return
    lines = (await s.execute(select(VoucherLine).where(
        VoucherLine.voucher_id == orig.id).order_by(VoucherLine.id))).scalars().all()
    # 원 전표를 그대로 복제하되 외화 GL만 통화 전용 계정으로 치환
    dtos = [VoucherLineDTO(GL_JPY if line.account_code == "1115" else line.account_code,
                           line.side, float(line.amount),
                           department_id=line.department_id) for line in lines]
    # 원 전기월이 마감이면 그 일자로 못 넣는다 → 개방기간(오늘)로 재전기.
    v = await vs.create(
        s, source_module="demo-ifrs", source_ref=ref, voucher_on=date.today(), lines=dtos,
        memo=(f"JPY 검진권 발행 JPY50,000 @9.0 (TEST-외화JPY·GL {GL_JPY} 재배치 "
              f"— 원 전기 #{orig.id} {orig.voucher_on} 마감기간이라 개방기간 전기)"))
    accs = " / ".join(f"{d.account_code} {d.side}" for d in dtos)
    print(f"  · {GL_JPY}로 재전기 → #{v.id} ({accs})")


async def backfill_ar_gl(s, vs) -> None:
    """미러만 있고 GL이 없던 demo-ifrs 채권을 백필(멱등).

    billed_on이 전부 마감 기간이라 개방기간(오늘)로 전기한다. 상대계정은 이익잉여금(3300) —
    과거 기간 발생분이므로 당월 수익으로 잡지 않는다.
    """
    from erp.finance.models import Receivable

    rows = (await s.execute(select(Receivable).where(
        Receivable.source_module == "demo-ifrs").order_by(Receivable.id))).scalars().all()
    if not rows:
        print("  · demo-ifrs 채권 없음 — 건너뜀")
        return
    for r in rows:
        ref = f"seed-ar-gl-backfill:{r.id}"
        if await s.scalar(select(Voucher).where(Voucher.source_ref == ref)):
            print(f"  · 채권 #{r.id} 이미 백필됨 — 건너뜀")
            continue
        amt = round(float(r.amount))
        v = await vs.create(
            s, source_module="demo-ifrs", source_ref=ref, voucher_on=date.today(),
            lines=[VoucherLineDTO(ACC_AR, "DEBIT", amt),
                   VoucherLineDTO(ACC_RETAINED, "CREDIT", amt)],
            memo=(f"시드 정합 — 미러만 있던 채권 GL 백필 ({r.party} {r.billed_on} {amt:,}) "
                  f"· 과거분이라 상대계정 이익잉여금"))
        print(f"  · 채권 #{r.id} {r.party} {amt:,} → 전표 #{v.id}")


async def main() -> None:
    vs = VoucherService()
    async with session_factory()() as s:
        print("① JPY 전용 GL 계정")
        await ensure_jpy_account(s)

        print("② JPY 법인계좌 GL 이관")
        await move_jpy_corp_account(s)

        print("③ JPY 데모 전기 재배치(1115 → 1116)")
        await rebook_jpy(s, vs)

        print("④ 오전기 환산손실 역분개")
        await reverse_once(s, vs, source_ref="fx-translate:8:2026-06",
                           label="외화환산(통화 혼입 오전기)")

        print("⑤ AR 미러 GL 백필")
        await backfill_ar_gl(s, vs)

        await s.commit()

    # 검증 — 조정 후 상태
    async with session_factory()() as s:
        rows = (await s.execute(select(
            CorporateAccount.id, CorporateAccount.currency,
            CorporateAccount.gl_account_code).where(
                CorporateAccount.is_active.is_(True)).order_by(CorporateAccount.id))).all()
        print("\n조정 후 법인계좌:")
        for i, cur, gl in rows:
            print(f"  #{i} {cur} → GL {gl}")


if __name__ == "__main__":
    asyncio.run(main())
