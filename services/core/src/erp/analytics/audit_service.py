"""회계 감사·이상탐지 (forensic/audit analytics) — ADR D-7 v1.

읽기 전용 — 전기완료(POSTED) 전표를 분석. 신규 테이블·외부 의존성 없음(순수 SQL/Python).
세 가지 검사: ① Benford 법칙(금액 첫자리 분포) ② 중복전표/중복지급 ③ 비정상 분개(주말·심야·라운드·한도직전).
모두 "리뷰 후보"를 제시할 뿐 자동 판정하지 않음 — 회계 담당자 확인이 최종.
"""
import hashlib
import math
from datetime import date, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.analytics.audit_review_service import AuditReviewService
from erp.finance.models import Voucher, VoucherLine

# Benford 기대 첫자리 빈도 — P(d) = log10(1 + 1/d)
BENFORD_EXP = {d: math.log10(1 + 1 / d) for d in range(1, 10)}
# Nigrini MAD 부합 임계 (1st digit)
_MAD_BANDS = ((0.006, "근접 부합"), (0.012, "허용 가능"), (0.015, "한계"))
_CHI2_CRIT_DF8 = 15.507   # χ² 임계(자유도 8, α=0.05)
_KST = timezone(timedelta(hours=9))


def _dup_key(voucher_ids: list[int]) -> str:
    """중복 클러스터 안정 식별자 — 멤버(전표 id 집합) 기준. 멤버 변동 시 새 후보로 재부상."""
    h = hashlib.sha1(",".join(str(i) for i in sorted(voucher_ids)).encode()).hexdigest()
    return f"dup:{h}"


def _is_open(review: dict | None) -> bool:
    """검토완료(REVIEWED)가 아니면 미검토."""
    return not review or review.get("status") != "REVIEWED"


class AuditService:
    """전표 데이터 기반 감사·이상탐지. 경영진 권한(analytics.reports)로 노출."""

    def __init__(self):
        self._reviews = AuditReviewService()

    async def benford(self, session: AsyncSession, *, date_from: date, date_to: date,
                      side: str | None = "DEBIT") -> dict:
        """Benford 법칙(첫자리) — 금액 분포가 자연 분포에서 벗어나면 조작·오류 신호.

        side=DEBIT 기본(지출·자산 증가 위주). χ²·MAD로 부합도 산출.
        """
        q = (select(VoucherLine.amount)
             .join(Voucher, Voucher.id == VoucherLine.voucher_id)
             .where(Voucher.status == "POSTED",
                    Voucher.voucher_on >= date_from, Voucher.voucher_on <= date_to))
        if side:
            q = q.where(VoucherLine.side == side)
        amounts = (await session.scalars(q)).all()

        counts = {d: 0 for d in range(1, 10)}
        n = 0
        for a in amounts:
            iv = int(abs(a))
            if iv == 0:
                continue
            lead = int(str(iv)[0])
            if lead == 0:
                continue
            counts[lead] += 1
            n += 1

        digits, chi2, mad = [], 0.0, 0.0
        for d in range(1, 10):
            exp_p = BENFORD_EXP[d]
            exp_c = exp_p * n
            obs_c = counts[d]
            obs_p = obs_c / n if n else 0.0
            if exp_c:
                chi2 += (obs_c - exp_c) ** 2 / exp_c
            mad += abs(obs_p - exp_p)
            digits.append({"digit": d, "observed": obs_c,
                           "observed_pct": round(obs_p * 100, 2),
                           "expected_pct": round(exp_p * 100, 2),
                           "deviation_pct": round((obs_p - exp_p) * 100, 2)})
        mad = mad / 9 if n else 0.0
        # 표본이 없으면 mad=0 → 최상 등급 오판 → N/A. 또한 표본<300은 MAD/χ² 변동이 커
        # 적합/부적합 판정이 통계적으로 무의미(경영 대시보드 "부적합" 오해 방지) → 표본부족 표시.
        if n == 0:
            verdict = "표본 없음(N/A)"
        elif n < 300:
            verdict = "표본부족(N/A)"
        else:
            verdict = next((label for thr, label in _MAD_BANDS if mad <= thr), "부적합")
        # 기대比 가장 크게 초과한 자리 = 우선 검토 대상
        digits_sorted = sorted(digits, key=lambda x: -x["deviation_pct"])
        return {
            "range": {"from": date_from.isoformat(), "to": date_to.isoformat()},
            "side": side, "sample_size": n,
            "chi_square": round(chi2, 2), "chi_square_critical": _CHI2_CRIT_DF8,
            "chi_square_exceeds": bool(n and chi2 > _CHI2_CRIT_DF8),
            "mad": round(mad, 5), "verdict": verdict,
            "digits": digits, "top_deviations": digits_sorted[:3],
            "note": "표본<300 시 통계적 신뢰도 낮음 — 참고용" if n < 300 else None,
        }

    async def duplicates(self, session: AsyncSession, *, date_from: date, date_to: date,
                         window_days: int = 7, side: str | None = None,
                         require_detail: bool = True, unreviewed_only: bool = False,
                         limit: int = 100) -> dict:
        """중복전표/중복지급 후보 — 동일 (계정·차대·금액·적요)가 window_days 내 2건 이상.

        require_detail=True(기본): 적요(거래처) 없는 그룹은 제외 — 거래처 신호 없는
        '동일 금액 반복'은 본인부담금 등 정상분개일 가능성이 커 오탐 과다.
        정기 반복분개(임차료 등) 오탐 가능 → '리뷰 후보'. 금액순 상위 limit.
        window는 클러스터 최초~최종 간격으로 판정(보수적 — 일부만 인접 시 누락 가능).
        """
        q = (select(VoucherLine.account_code, VoucherLine.side, VoucherLine.amount,
                    VoucherLine.detail, Voucher.id, Voucher.voucher_on,
                    Voucher.source_module, Voucher.source_ref)
             .join(Voucher, Voucher.id == VoucherLine.voucher_id)
             .where(Voucher.status == "POSTED",
                    Voucher.voucher_on >= date_from, Voucher.voucher_on <= date_to))
        if side:
            q = q.where(VoucherLine.side == side)
        rows = (await session.execute(q)).all()

        groups: dict[tuple, dict] = {}
        for acct, sd, amt, detail, vid, von, smod, sref in rows:
            if require_detail and not (detail or "").strip():
                continue                      # 거래처(적요) 없는 동일금액은 중복 판정 제외
            key = (acct, sd, int(amt), detail or "")
            g = groups.setdefault(key, {"vouchers": {}})
            # 전표 단위 dedup(한 전표가 같은 키 라인 복수여도 1건)
            g["vouchers"][vid] = {"voucher_id": vid, "voucher_on": von,
                                  "source": f"{smod}:{sref}"}

        clusters = []
        for (acct, sd, amt, detail), g in groups.items():
            vs = sorted(g["vouchers"].values(), key=lambda x: x["voucher_on"])
            if len(vs) < 2:
                continue
            span = (vs[-1]["voucher_on"] - vs[0]["voucher_on"]).days
            if span > window_days:
                continue
            clusters.append({
                "finding_key": _dup_key([v["voucher_id"] for v in vs]),
                "account_code": acct, "side": sd, "amount": amt,
                "detail": detail or None, "count": len(vs), "span_days": span,
                "vouchers": [{"voucher_id": v["voucher_id"],
                              "voucher_on": v["voucher_on"].isoformat(),
                              "source": v["source"]} for v in vs],
            })
        rmap = await self._reviews.map_for(
            session, finding_type="DUPLICATE", keys=[c["finding_key"] for c in clusters])
        for c in clusters:
            c["review"] = rmap.get(c["finding_key"])
        open_count = sum(1 for c in clusters if _is_open(c["review"]))
        if unreviewed_only:
            clusters = [c for c in clusters if _is_open(c["review"])]
        clusters.sort(key=lambda c: (-c["amount"], -c["count"]))
        return {
            "range": {"from": date_from.isoformat(), "to": date_to.isoformat()},
            "window_days": window_days, "side": side,
            "cluster_count": len(clusters), "open_count": open_count,
            "duplicate_amount": sum(c["amount"] * (c["count"] - 1) for c in clusters),
            "clusters": clusters[:limit],
        }

    async def abnormal_journals(self, session: AsyncSession, *, date_from: date,
                                date_to: date, threshold: float | None = None,
                                round_unit: int = 1_000_000,
                                source_module: str | None = "manual",
                                unreviewed_only: bool = False, limit: int = 200) -> dict:
        """비정상 분개 후보 — 주말·심야 입력·라운드넘버·승인한도 직전.

        source_module="manual"(기본): **사람이 입력한** 수동전표만 검사 — 자동전표(감가상각·
        일마감·청구sync)는 야간·주말 배치 전기가 정상이라 타이밍 검사 대상이 아님.
        주말·심야 판정은 voucher_on(회계일)이 아닌 created_at(**입력 시각**) 기준 — 회계일은
        월말 등 주말일 수 있으나 정상. created_at 없으면(레거시) 타이밍 검사 생략.
        threshold 지정 시 그 90~100% 구간 금액을 '한도 직전 분할' 의심으로 표시.
        """
        q = (select(Voucher.id, Voucher.voucher_on, Voucher.created_at,
                    Voucher.source_module, Voucher.source_ref, Voucher.memo,
                    func.max(VoucherLine.amount))
             .join(VoucherLine, VoucherLine.voucher_id == Voucher.id)
             .where(Voucher.status == "POSTED",
                    Voucher.voucher_on >= date_from, Voucher.voucher_on <= date_to)
             .group_by(Voucher.id, Voucher.voucher_on, Voucher.created_at,
                       Voucher.source_module, Voucher.source_ref, Voucher.memo))
        if source_module:
            q = q.where(Voucher.source_module == source_module)
        rows = (await session.execute(q)).all()

        flagged = []
        reason_counts = {"weekend": 0, "offhours": 0, "round": 0, "below_threshold": 0}
        for vid, von, created, smod, sref, memo, max_amt in rows:
            amt = int(max_amt or 0)
            reasons = []
            if created is not None:                                  # 입력 시각 기준 타이밍
                dt = created.astimezone(_KST) if created.tzinfo else created
                if dt.weekday() >= 5:                                # 토(5)·일(6) 입력
                    reasons.append("weekend")
                if dt.hour < 7 or dt.hour >= 22:                     # 심야 22:00~07:00 입력
                    reasons.append("offhours")
            if amt >= round_unit and amt % round_unit == 0:          # 큰 라운드넘버
                reasons.append("round")
            if threshold and threshold * 0.9 <= amt < threshold:     # 한도 직전(90~100%)
                reasons.append("below_threshold")
            if not reasons:
                continue
            for r in reasons:
                reason_counts[r] += 1
            flagged.append({"finding_key": f"voucher:{vid}", "voucher_id": vid,
                            "voucher_on": von.isoformat(), "amount": amt,
                            "source": f"{smod}:{sref}", "memo": memo, "reasons": reasons})
        rmap = await self._reviews.map_for(
            session, finding_type="ABNORMAL", keys=[f["finding_key"] for f in flagged])
        for f in flagged:
            f["review"] = rmap.get(f["finding_key"])
        open_count = sum(1 for f in flagged if _is_open(f["review"]))
        if unreviewed_only:
            flagged = [f for f in flagged if _is_open(f["review"])]
        flagged.sort(key=lambda x: (-len(x["reasons"]), -x["amount"]))
        return {
            "range": {"from": date_from.isoformat(), "to": date_to.isoformat()},
            "threshold": threshold, "round_unit": round_unit, "source_module": source_module,
            "flagged_count": len(flagged), "open_count": open_count,
            "by_reason": reason_counts, "items": flagged[:limit],
        }

    async def summary(self, session: AsyncSession, *, date_from: date,
                      date_to: date, threshold: float | None = None) -> dict:
        """감사 대시보드 헤드라인 — 세 검사 요약 카운트(드릴은 개별 EP)."""
        bf = await self.benford(session, date_from=date_from, date_to=date_to)
        du = await self.duplicates(session, date_from=date_from, date_to=date_to)
        ab = await self.abnormal_journals(session, date_from=date_from, date_to=date_to,
                                          threshold=threshold)
        return {
            "range": {"from": date_from.isoformat(), "to": date_to.isoformat()},
            "benford": {"verdict": bf["verdict"], "mad": bf["mad"],
                        "chi_square_exceeds": bf["chi_square_exceeds"],
                        "sample_size": bf["sample_size"]},
            "duplicates": {"cluster_count": du["cluster_count"], "open_count": du["open_count"],
                           "duplicate_amount": du["duplicate_amount"]},
            "abnormal": {"flagged_count": ab["flagged_count"], "open_count": ab["open_count"],
                         "by_reason": ab["by_reason"]},
            # 미검토 후보 합계(Benford 제외 — 분포 신호는 항목 검토 대상 아님)
            "open_total": du["open_count"] + ab["open_count"],
        }
