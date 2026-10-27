"""Claims 유스케이스 — 업로드 검증(§6.7)·이의신청 기한(FR-40)·손실 전표·삭감 통계(FR-48).

G1 Gate: 통보서 파싱·매핑은 실물 샘플 확보 전 구현 금지 — 스테이징 등록까지만.
"""
import re
import uuid
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.claims.models import (
    Appeal, ClaimAdjustment, ClaimBatch, ClaimStatement, NoticeFileStaging,
)
from erp.finance.service import VoucherLineDTO, VoucherService

_audit = AuditService()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024            # §6.7 — 50MB
ALLOWED_MAGIC = {
    b"PK\x03\x04": ".zip/.xlsx",                # zip 계열
    b"%PDF": ".pdf",
    b"\xd0\xcf\x11\xe0": ".hwp/.xls(OLE)",      # OLE2
}
SUPPLEMENT_DEADLINE_YEARS = 3                   # 지급불능 보완청구
ALERT_DAYS = (14, 7, 3)                         # D-임박 알림

ACC_LOSS = "5910"      # 삭감 손실 (잡손실)
ACC_RECEIVABLE = "1140"  # 보험 미수금
ACC_CASH = "1110"      # 현금·예금 (공단 지급 입금)
ACC_ADVANCE = "2141"   # 선수금(공단) — 결정액 잔여 초과 지급분(과다지급) 분리 전기
PAYMENT_REF_MAX = 64 - len("claim:payment:")   # 전표 키 VARCHAR(64) 상한 역산(=50)

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _require_month(care_month: str) -> str:
    """진료월 YYYY-MM 검증·정규화 — 형식 오류 시 care_month 필터가 조용히 미대사되는 것 방지."""
    m = (care_month or "").strip()
    if not _MONTH_RE.match(m):
        raise ValidationError("진료월(care_month)은 YYYY-MM 형식이어야 합니다")
    return m


def _allowed_text(head: bytes) -> bool:
    """텍스트형(EDI 전문·CSV) 허용 — 제어 문자 비중 검사."""
    sample = head[:512]
    if not sample:
        return False
    ctrl = sum(1 for b in sample if b < 9 or (13 < b < 32))
    return ctrl / len(sample) < 0.05


class NoticeUploadService:
    """통보서 파일 업로드 (fallback 필수 경로 — FR-38)."""

    def __init__(self, storage_dir: str = "staging-files"):
        self._dir = Path(storage_dir)

    async def upload(self, session: AsyncSession, *, actor: int, original_name: str,
                     content: bytes) -> NoticeFileStaging:
        # §6.7 — 크기·매직바이트 검증, UUID 재명명 (경로 탐색 차단)
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValidationError("파일이 50MB를 초과합니다")
        if len(content) == 0:
            raise ValidationError("빈 파일입니다")
        head = content[:8]
        if not any(head.startswith(m) for m in ALLOWED_MAGIC) and not _allowed_text(head):
            raise ValidationError("허용되지 않는 파일 형식입니다 (PDF/XLSX/HWP/텍스트 전문만)")

        stored_name = uuid.uuid4().hex
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / stored_name).write_bytes(content)  # 저장 경로 = 고정 prefix + UUID만

        staging = NoticeFileStaging(stored_name=stored_name,
                                    original_name=original_name[:200],
                                    size_bytes=len(content), uploaded_by=actor)
        session.add(staging)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="UPLOAD",
                                  entity="integration.hira_notice_staging",
                                  target_id=str(staging.id),
                                  after={"original_name": original_name,
                                         "size": len(content)})
        # G1: 파싱은 실물 레이아웃 확보 후 worker-batch 잡으로 — 여기서는 staging까지
        return staging


class AppealService:
    """이의신청 워크플로우 (FR-40) — 기한 자동 계산·확정 손실 전표."""

    def __init__(self, vouchers: VoucherService | None = None):
        self._vouchers = vouchers or VoucherService()

    @staticmethod
    def legal_deadline(notice_arrived_on: date, disposed_on: date | None = None,
                       extra_dates: set[date] | None = None) -> date:
        """법령 기반 (G5 확정) — 건보법 §87 90일 + 처분일 180일 상한 + 민법 §157/§161.
        extra_dates: DB 공휴일 캘린더(변동 공휴일) 주입 — 없으면 코드 폴백."""
        from erp.claims.deadline import appeal_deadline
        return appeal_deadline(notice_arrived_on, disposed_on=disposed_on, extra_dates=extra_dates)

    async def open(self, session: AsyncSession, *, adjustment_id: int, appeal_type: str,
                   notice_arrived_on: date, actor: int) -> Appeal:
        # 2026-10-09 시나리오 S-B: 같은 조정건에 POST /claims/appeals 4건을 동시에 보내자 아래 중복 검사를
        # 모두 통과해 201이 여러 번 났고, 남는 이의를 종결할 때마다 복원돼 5910이 음수가 됐다(check-then-insert
        # 경쟁). 조정 행을 FOR UPDATE로 잠가 같은 조정건의 개시를 직렬화한다(PG·sqlite는 무시 — 단일 기록자).
        adj = (await session.execute(
            select(ClaimAdjustment).where(ClaimAdjustment.id == adjustment_id).with_for_update()
        )).scalar_one_or_none()
        if adj is None:
            raise ValidationError("존재하지 않는 조정 내역입니다")
        # 처분일(통보서 결정일) 조회 — 법 §87 180일 상한 (Check#4 권장 ①)
        from erp.claims.models import ClaimBatch
        disposed_on = (await session.execute(
            select(ClaimBatch.decided_on)
            .join(ClaimStatement, ClaimStatement.batch_id == ClaimBatch.id)
            .where(ClaimStatement.id == adj.statement_id))).scalar_one_or_none()
        from erp.common.holidays import HolidayService
        holidays = await HolidayService().dates(session)   # DB 공휴일 캘린더(변동분·없으면 코드 폴백)
        deadline = self.legal_deadline(notice_arrived_on, disposed_on, extra_dates=holidays or None)
        if date.today() > deadline:
            raise ValidationError("법정 기한(90일)이 경과했습니다",
                                  {"deadline": deadline.isoformat()})  # ERP-3004
        # 같은 조정건에 진행 중·인정된 이의신청이 있으면 새로 열지 않는다(기각 후 상위 이의는 허용) —
        # 종전엔 중복 개시·각각 인정으로 1140이 두 번 복원되고 5910이 음수(이익)가 됐다(2026-10-09 검토 B).
        dup = await session.scalar(select(Appeal.id).where(
            Appeal.adjustment_id == adjustment_id, Appeal.status != "REJECTED"))
        if dup is not None:
            raise ValidationError("이 조정건에 진행 중이거나 인정된 이의신청이 있습니다",
                                  {"appeal_id": dup})
        appeal = Appeal(adjustment_id=adjustment_id, appeal_type=appeal_type,
                        notice_arrived_on=notice_arrived_on, legal_deadline=deadline,
                        created_by=actor)
        session.add(appeal)
        await session.flush()
        return appeal

    async def transition(self, session: AsyncSession, *, appeal_id: int, status: str,
                         actor: int, final_loss_amt: float | None = None) -> Appeal:
        """상태 전이 (접수→보완→기각/인정). 종결 시 삭감손실 델타만 전기.

        **결정시점 인식 모델(#621 SoT·설계결정 2026-07-13)**: 삭감손실(5910/1140)은 이미
        his.claim `claim.reviewed`에서 인식된다. 항소는 그 인식분을 SoT로 삼아 **최종 손실과의
        차액(델타)만** 전기한다 — 기각(삭감 확정)=추가 전표 없음(과거 이중계상 제거), 인정(삭감
        취소)=원 삭감 역분개(DR 1140/CR 5910)로 미수 복원 → 이후 공단 재지급이 1140 소멸.
        전제: 항소 대상 삭감은 review에서 인식됨(MEDRLT ingest는 전표 없음). review 인식액은
        조정내역 adj_amt로 근사(#621 두 소스 비연계라 FK 링크 부재)."""
        # 같은 이의신청의 동시 종결(상태 검사→전이 경쟁)과 같은 조정건 종결 간의 직전 최종손실 읽기를
        # 직렬화 — 이의 행·조정 행을 잠근다(2026-10-09 시나리오 S-B·open과 같은 잠금 순서: 조정 행).
        appeal = (await session.execute(
            select(Appeal).where(Appeal.id == appeal_id).with_for_update())).scalar_one_or_none()
        if appeal is None:
            raise ValidationError("존재하지 않는 이의신청입니다")
        await session.execute(select(ClaimAdjustment.id).where(
            ClaimAdjustment.id == appeal.adjustment_id).with_for_update())
        valid_next = {"RECEIVED": {"SUPPLEMENT", "REJECTED", "ACCEPTED"},
                      "SUPPLEMENT": {"REJECTED", "ACCEPTED"}}
        if status not in valid_next.get(appeal.status, set()):
            raise ValidationError("허용되지 않는 상태 전이",
                                  {"from": appeal.status, "to": status})
        if status in ("REJECTED", "ACCEPTED") and final_loss_amt is not None:
            # 상태를 바꾸기 **전에** 검증 — 검증 실패가 상태만 바꿔 놓고 전표는 없는 반쪽 전이를 남기지 않게
            adj0 = await session.get(ClaimAdjustment, appeal.adjustment_id)
            if not 0 <= float(final_loss_amt) <= float(adj0.adj_amt):
                raise ValidationError("최종 손실액은 0 이상 삭감액 이하여야 합니다",
                                      {"booked": float(adj0.adj_amt)})
        appeal.status = status

        if status in ("REJECTED", "ACCEPTED"):
            adj = (await session.execute(
                select(ClaimAdjustment).where(ClaimAdjustment.id == appeal.adjustment_id)
            )).scalar_one()
            # review에서 인식된 삭감손실(근사)에서 **같은 조정건의 이전 종결이 이미 복원한 금액**을 뺀다 —
            # 부분 기각(최종손실<삭감액) 뒤 상위 이의가 인정되면 adj_amt 기준으로 다시 복원해 이중 복원됐다
            # (2차 검토 B).
            # 최종손실은 **누적 절대값**(그 시점까지의 남은 삭감)이다 — 이전 종결들의 복원을 합산하면 3차 이상
            # 이의에서 틀어졌다(3차 검토 B). 직전 종결 이의(가장 최근 id)의 최종손실이 현재 인식된 손실이다.
            last_fl = await session.scalar(
                select(Appeal.final_loss_amt).where(
                    Appeal.adjustment_id == adj.id, Appeal.id != appeal.id,
                    Appeal.status.in_(("ACCEPTED", "REJECTED")),
                    Appeal.final_loss_amt.isnot(None))
                .order_by(Appeal.id.desc()).limit(1))
            booked = float(last_fl) if last_fl is not None else float(adj.adj_amt)
            # 최종 손실: 기각=삭감 유지(기본 booked), 인정=삭감 취소(기본 0). partial은 final_loss_amt.
            if final_loss_amt is not None:
                final_loss = float(final_loss_amt)   # 범위는 위에서 검증(음수·과대 → 1140 과대 복원 차단)
            else:
                final_loss = booked if status == "REJECTED" else 0.0
            appeal.final_loss_amt = final_loss
            delta = round(final_loss - booked)   # +면 추가 손실, −면 삭감 취소(미수 복원)
            if delta < 0:                        # 인정 — 삭감 취소·미수 복원 (DR 1140 / CR 5910)
                lines = [VoucherLineDTO(ACC_RECEIVABLE, "DEBIT", -delta),
                         VoucherLineDTO(ACC_LOSS, "CREDIT", -delta)]
                memo = f"이의신청 인정 — 삭감 취소·미수 복원 (appeal #{appeal.id})"
            elif delta > 0:                      # 추가 삭감 손실(드묾) — DR 5910 / CR 1140
                lines = [VoucherLineDTO(ACC_LOSS, "DEBIT", delta),
                         VoucherLineDTO(ACC_RECEIVABLE, "CREDIT", delta)]
                memo = f"이의신청 — 추가 삭감 손실 확정 (appeal #{appeal.id})"
            else:                                # 기각(삭감 유지) — review 인식분과 동일, 전표 없음
                lines = None
            if lines is not None:
                voucher = await self._vouchers.create(
                    session, source_module="claims", source_ref=f"appeal-adj-{appeal.id}",
                    voucher_on=date.today(), lines=lines, memo=memo, created_by=actor)
                appeal.voucher_ref = f"voucher-{voucher.id}"
        await _audit.record_audit(session, actor=actor, action="TRANSITION",
                                  entity="claims.appeals", target_id=str(appeal.id),
                                  after={"status": status})
        return appeal

    async def deadline_alert_targets(self, session: AsyncSession, *,
                                     today: date) -> list[Appeal]:
        """D-14/7/3 임박 알림 대상 (미종결 건) — worker-batch 잡 입력.

        정확일치(==today+d)는 배치 1일 누락(휴일·장애) 시 그날 D-d 걸리는 건을 영구 소실시킨다.
        임계 이하(≤) 판정 + alert_stage(마지막 발송 버킷) 멱등으로 누락 없이 버킷당 1회만 발송한다.
        alert_stage=이미 발송한 가장 작은 D-버킷 → 더 임박한 버킷 진입 시에만 재발송.
        """
        buckets = sorted(ALERT_DAYS)                    # [3, 7, 14]
        rows = (await session.execute(
            select(Appeal).where(
                Appeal.status.in_(["RECEIVED", "SUPPLEMENT"]),
                Appeal.legal_deadline <= today + timedelta(days=buckets[-1]),
                Appeal.legal_deadline >= today))     # 도과분은 알림 대상 아님(별도 처리)
            ).scalars().all()
        targets = []
        for a in rows:
            days = (a.legal_deadline - today).days
            bucket = next((d for d in buckets if days <= d), None)   # 진입한 가장 작은 임계
            if bucket is None:
                continue
            if a.alert_stage is None or bucket < a.alert_stage:      # 첫 진입 or 더 임박한 버킷
                a.alert_stage = bucket
                targets.append(a)
        return targets


class ClaimStatsService:
    """삭감 통계 (FR-48) — caller_scope 명시 인자 (design §6.2)."""

    async def by_dimension(self, session: AsyncSession, *, by: str,
                           scope_dept_codes: list[str] | None = None) -> list[dict]:
        if by == "reason":
            return await self._by_reason(session, scope_dept_codes=scope_dept_codes)
        col = {"dept": ClaimStatement.dept_code,
               "doctor": ClaimStatement.doctor_id}.get(by)
        if col is None:
            raise ValidationError("by는 dept/doctor/reason만 허용됩니다")
        q = (select(col.label("dim"),
                    func.count(ClaimAdjustment.id).label("cnt"),
                    func.coalesce(func.sum(ClaimAdjustment.adj_amt), 0).label("amt"))
             .join(ClaimAdjustment, ClaimAdjustment.statement_id == ClaimStatement.id)
             .group_by(col))
        if scope_dept_codes is not None:  # 부서장 scope — dept_code 후보(부서명/코드) IN 경계(§6.2)
            q = q.where(ClaimStatement.dept_code.in_(scope_dept_codes))
        rows = (await session.execute(q)).all()
        return [{"dim": str(r.dim), "count": int(r.cnt), "amount": float(r.amt)} for r in rows]

    async def _by_reason(self, session: AsyncSession, *,
                         scope_dept_codes: list[str] | None = None) -> list[dict]:
        """사유 대분류별 삭감 통계 (FR-39) — 조정코드 첫 알파벳 그룹핑."""
        from erp.claims.adj_codes import categorize
        q = (select(func.substr(ClaimAdjustment.adj_code, 1, 1).label("letter"),
                    func.count(ClaimAdjustment.id).label("cnt"),
                    func.coalesce(func.sum(ClaimAdjustment.adj_amt), 0).label("amt"))
             .join(ClaimStatement, ClaimStatement.id == ClaimAdjustment.statement_id)
             .group_by("letter"))
        if scope_dept_codes is not None:
            q = q.where(ClaimStatement.dept_code.in_(scope_dept_codes))
        rows = (await session.execute(q)).all()
        out = []
        for r in rows:
            letter, label = categorize(r.letter)
            out.append({"dim": f"{letter} — {label}", "count": int(r.cnt),
                        "amount": float(r.amt)})
        return sorted(out, key=lambda x: -x["amount"])

    async def automation_rate(self, session: AsyncSession) -> dict:
        """자동화율 = AUTO 매핑 조정 내역 ÷ 전체 (Plan §4.3 분모/분자 정의)."""
        from sqlalchemy import case as sa_case
        row = (await session.execute(   # 단일 스캔 — 전체·AUTO 동시 집계(2회 풀카운트 제거)
            select(func.count(),
                   func.coalesce(func.sum(
                       sa_case((ClaimAdjustment.map_status == "AUTO", 1), else_=0)), 0))
            .select_from(ClaimAdjustment))).one()
        total, auto = int(row[0]), int(row[1])
        return {"total": total, "auto": auto,
                "rate": round(auto / total * 100, 1) if total else 0.0}


class ClaimPaymentService:
    """공단 지급 대사 (심사청구 회계 완결) — 지급명세 입금을 보험미수금(1140) 소멸로 전기.

    발생(claim.submitted DR1140)·삭감(claim.reviewed CR1140) 이후 공단 실지급이 AR을 소멸시키지
    못하던 갭 해소. 지급명세(payment_ref) 단위 멱등 입금 대사 + 결정액 대비 variance(과소지급).
    """

    def __init__(self, vouchers: VoucherService | None = None):
        self._vouchers = vouchers or VoucherService()

    async def _decided_for_month(self, session: AsyncSession, care_month: str) -> float | None:
        """진료월 결정액 합(공단 지급 예상 기준) — MEDRLT 결정분. 결정 데이터 없으면 None(대사 보류)."""
        from erp.claims.models import ClaimBatch, ClaimStatement
        cnt, total = (await session.execute(
            select(func.count(ClaimStatement.id),
                   func.coalesce(func.sum(ClaimStatement.decided_amt), 0))
            .join(ClaimBatch, ClaimBatch.id == ClaimStatement.batch_id)
            .where(ClaimBatch.care_month == care_month,
                   ClaimStatement.decided_amt.isnot(None)))).one()
        return float(total) if int(cnt) > 0 else None

    async def _expected_for_month(self, session: AsyncSession, care_month: str) -> float | None:
        """공단 지급 기대액 = MEDRLT 결정액 + 이의신청 인정 복원액. 결정 데이터가 없으면 None.

        2026-10-09 시나리오 S-B: 이의 인정으로 1140이 복원(DR 1140/CR 5910)돼 GL은 맞는데, 지급 대사·
        variance·배치 상태는 MEDRLT 결정액만 봐서 복원분 재지급을 '과다지급(선수금)·차이'로 표시했다.
        인식 AR(_recognized_ar)과 같은 복원 기준(_appeal_restored)을 더한다."""
        decided = await self._decided_for_month(session, care_month)
        if decided is None:
            return None
        return decided + await self._appeal_restored(session, care_month)

    async def _recognized_ar(self, session: AsyncSession, care_month: str) -> float | None:
        """진료월 인식 보험미수금(1140 GL 대응) — 제출 청구(ClaimSubmission) 기준.

        1140은 his.claim 이벤트(submit DR=청구액 / review CR=삭감)로만 구동되고 MEDRLT 결정액과
        소스가 분리(#621)돼 있다. 과다지급 2141 분리를 MEDRLT decided_amt로 판정하면 (a) 결정 전
        선지급, (b) 결정액 상향(재통보) 시 1140을 상쇄 차변 없이 음수(대변잔액)로 몰아 2141 분리가
        우회되던 결함이 있었다. 실제 1140을 재현하는 제출 청구 기준(결정분=승인액 decided_amt·
        미결정분=청구액 claimed_amt)으로 초과분을 산정한다. 진료월 캡처가 없으면(care_month 미동봉
        또는 his.claim 웹훅 미연동) None → 호출부는 MEDRLT decided로 폴백(현행 동작 보존)."""
        from erp.claims.models import ClaimSubmission
        cnt, total = (await session.execute(
            select(func.count(ClaimSubmission.id),
                   func.coalesce(func.sum(case(
                       (ClaimSubmission.decided_on.isnot(None),
                        func.coalesce(ClaimSubmission.decided_amt, 0)),
                       else_=ClaimSubmission.claimed_amt)), 0))
            .where(ClaimSubmission.care_month == care_month))).one()
        if int(cnt) == 0:
            return None
        # 이의신청 인정으로 복원된 1140(DR 1140/CR 5910)도 인식 AR이다 — 빠지면 공단 재지급이 1140 대신
        # 2141(선수금)로 오분리돼 두 계정에 같은 금액이 영구 잔존했다(2026-10-09 검토 B).
        return float(total) + await self._appeal_restored(session, care_month)

    async def _appeal_restored(self, session: AsyncSession, care_month: str) -> float:
        """진료월 이의신청 인정 누적 복원액(DR 1140/CR 5910) — 인식 AR·지급 대사 기대액의 단일 기준.

        조정건별 누적 복원 = 삭감액 − **가장 최근 종결** 이의의 최종손실(누적 절대값·transition과 같은 기준)."""
        latest = (select(Appeal.adjustment_id, func.max(Appeal.id).label("aid"))
                  .where(Appeal.status.in_(("ACCEPTED", "REJECTED")), Appeal.final_loss_amt.isnot(None))
                  .group_by(Appeal.adjustment_id)).subquery()
        per_adj = (select(ClaimAdjustment.adj_amt.label("amt"),
                          Appeal.final_loss_amt.label("fl"))
                   .select_from(Appeal)
                   .join(latest, latest.c.aid == Appeal.id)
                   .join(ClaimAdjustment, ClaimAdjustment.id == Appeal.adjustment_id)
                   .join(ClaimStatement, ClaimStatement.id == ClaimAdjustment.statement_id)
                   .join(ClaimBatch, ClaimBatch.id == ClaimStatement.batch_id)
                   .where(ClaimBatch.care_month == care_month)).subquery()
        restored = await session.scalar(
            select(func.coalesce(func.sum(per_adj.c.amt - per_adj.c.fl), 0)))
        return max(float(restored or 0), 0.0)

    async def record(self, session: AsyncSession, *, actor: int, care_month: str,
                     payment_ref: str, paid_on: date, paid_amt: float,
                     note: str | None = None):
        """공단 지급 대사 기록 — DR 1110 현금/CR 1140 보험미수금 전기 + variance. payment_ref 멱등."""
        from erp.claims.models import ClaimPayment
        if paid_amt <= 0:
            raise ValidationError("지급액은 0보다 커야 합니다")
        # 진입점 1회 정규화 — 멱등 SELECT·voucher source_ref·저장 컬럼이 모두 동일 정규형을 쓰게 해
        # 앞뒤 공백만 다른 재전송("PAY-1"↔" PAY-1")이 dedup를 놓쳐 uq 제약 IntegrityError로 터지던 결함 방지.
        payment_ref = (payment_ref or "").strip()
        if not payment_ref:
            raise ValidationError("지급명세 식별(payment_ref)은 필수입니다")
        # 2026-10-09 시나리오 S-B: 전표 키 `claim:payment:{payment_ref}`가 finance.vouchers.source_ref
        # VARCHAR(64)를 넘으면 PG 절단 오류(500)였다. 해시 축약은 선수금 잔액 집계(_advance_balance)가
        # 이 키를 payment_ref로 재조립해 찾으므로 불가 — 입구에서 길이를 막는다(조치: 지급명세 번호 축약).
        if len(payment_ref) > PAYMENT_REF_MAX:
            raise ValidationError(
                f"지급명세 식별(payment_ref)은 {PAYMENT_REF_MAX}자 이하여야 합니다 — 입력자가 지급통지서의 "
                f"지급명세 번호만 입력해 주세요", {"length": len(payment_ref)})
        care_month = _require_month(care_month)
        existing = (await session.execute(
            select(ClaimPayment).where(ClaimPayment.payment_ref == payment_ref))
        ).scalar_one_or_none()
        if existing is not None:
            return existing                       # 멱등 — 동일 지급명세 재처리 안전
        # 동일 care_month 동시 record 직렬화(적대검토 L-2) — reclass_advance와 같은 advisory lock으로
        # paid_so_far 읽기→AR/2141 분할 계산을 순차화(둘이 같은 잔여를 읽어 1140 과대소멸/2141 오분할
        # 방지). reclass가 델타로 자가치유하나 순간 불일치·오분할을 근본 차단.
        await self._lock_advance(session, care_month)
        expected = await self._expected_for_month(session, care_month)
        # 당월 누적 지급(이번 건 제외) — variance·AR 잔여에 공통 사용(중복 조회 제거).
        paid_so_far = float(await session.scalar(
            select(func.coalesce(func.sum(ClaimPayment.paid_amt), 0))
            .where(ClaimPayment.care_month == care_month)) or 0)
        # variance = 월 결정액 − 누적지급(이번 건 포함) 잔여. 단건 지급을 월 결정액과 직접 비교하면
        # (과거) 부분지급마다 큰 과소지급이 잘못 표기(예 결정 90만·40만+50만 2회 지급 시 각 50만/40만
        # 표기, 완납 후에도 0이 안 됨)되던 것을 누적 기준으로 정합(전체검토 claims variance·GL 무관 정보성).
        variance = (round(expected - (paid_so_far + paid_amt))
                    if expected is not None else None)
        # 과다지급 분리 전기 — 인식 AR(1140 재현) 잔여를 넘는 지급분은 1140이 아니라 2141 선수금
        # (공단)으로. 넘치는 CR 1140은 보험미수금을 음수(대변잔액)로 오표시. 기준은 실제 1140을
        # 재현하는 제출청구 인식 AR(_recognized_ar) 우선 — 결정 전 선지급도 인식 AR 초과분을 즉시
        # 2141로 분리(음수 1140 방지). 제출청구 미연계면 MEDRLT decided로 폴백, 그마저 없으면
        # (expected None) 잔여 판정 불가라 현행대로 전액 1140(AR은 제출분이 이미 DR).
        paid_amt = round(paid_amt)
        ar_credit, adv_credit = paid_amt, 0
        ar_basis = await self._recognized_ar(session, care_month)
        if ar_basis is None:
            ar_basis = expected
        if ar_basis is not None:
            outstanding = max(0, round(ar_basis - paid_so_far))
            ar_credit = min(paid_amt, outstanding)
            adv_credit = paid_amt - ar_credit
        lines = [VoucherLineDTO(ACC_CASH, "DEBIT", paid_amt)]
        if ar_credit > 0:
            lines.append(VoucherLineDTO(ACC_RECEIVABLE, "CREDIT", ar_credit))
        if adv_credit > 0:
            lines.append(VoucherLineDTO(ACC_ADVANCE, "CREDIT", adv_credit))
        voucher = await self._vouchers.create(
            session, source_module="claims", source_ref=f"claim:payment:{payment_ref}",
            voucher_on=paid_on,                   # 실입금일(개방기간 전기 — 마감월 지각 지급도 통과)
            lines=lines,
            memo=f"공단 지급 대사 {care_month} ({payment_ref})", created_by=actor)
        pay = ClaimPayment(
            payment_ref=payment_ref, care_month=care_month, paid_on=paid_on,
            paid_amt=round(paid_amt),
            expected_amt=(round(expected) if expected is not None else None),
            variance=variance, note=(note or "").strip() or None,
            voucher_ref=f"voucher-{voucher.id}", created_by=actor)
        session.add(pay)
        await session.flush()
        await self.refresh_status(session, care_month)   # 지급 반영 → 배치 생애주기 상태 갱신
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="claims.claim_payments", target_id=str(pay.id),
                                  after={"care_month": care_month, "paid": round(paid_amt),
                                         "variance": variance})
        return pay

    async def refresh_status(self, session: AsyncSession, care_month: str) -> str:
        """진료월 배치 지급 생애주기 상태 갱신 — 결정액 대비 지급액으로 분류.

        지급 없음=DECIDED(미지급)·지급≥결정(결정>0)=PAID(지급완료)·그 외=PARTIALLY_PAID(부분지급).
        지급 대사(record)·통보서 인제스트(신규 배치) 후 호출해 월 단위로 일괄 갱신.
        """
        from erp.claims.models import ClaimBatch, ClaimPayment, ClaimStatement
        decided = float(await session.scalar(
            select(func.coalesce(func.sum(ClaimStatement.decided_amt), 0))
            .join(ClaimBatch, ClaimBatch.id == ClaimStatement.batch_id)
            .where(ClaimBatch.care_month == care_month)) or 0)
        decided += await self._appeal_restored(session, care_month)   # 이의 인정 복원분(대사와 같은 기준)
        paid = float(await session.scalar(
            select(func.coalesce(func.sum(ClaimPayment.paid_amt), 0))
            .where(ClaimPayment.care_month == care_month)) or 0)
        if paid <= 0:
            status = "DECIDED"
        elif decided > 0 and paid >= decided:
            status = "PAID"
        else:
            status = "PARTIALLY_PAID"
        for b in (await session.scalars(
                select(ClaimBatch).where(ClaimBatch.care_month == care_month))).all():
            b.status = status
        return status

    async def _lock_advance(self, session: AsyncSession, care_month: str) -> None:
        """진료월 선수금 상계 채번 직렬화 — 동시 :vN 계산 충돌(uq IntegrityError) 방지. sqlite no-op."""
        if session.bind.dialect.name == "postgresql":
            await session.execute(
                select(func.pg_advisory_xact_lock(0x41445643, func.hashtext(care_month))))  # 'ADVC'

    async def _advance_balance(self, session: AsyncSession, care_month: str) -> float:
        """진료월 선수금(2141) GL 잔액 — 순액(CR−DR). 지급전표(과다지급 분리) + 상계전표(양방향
        증액·감액) 전체를 합산. reclass가 양방향이 되며 상계전표도 CR/DR 모두 발생하므로 지급 CR만
        세던 방식은 오집계 — 진료월 관련 2141 전표 전량을 side 부호로 순액 계산한다."""
        from erp.claims.models import ClaimPayment
        from erp.finance.models import Voucher, VoucherLine
        pay_refs = select("claim:payment:" + ClaimPayment.payment_ref).where(
            ClaimPayment.care_month == care_month)
        net = float(await session.scalar(
            select(func.coalesce(func.sum(case(
                (VoucherLine.side == "CREDIT", VoucherLine.amount),
                else_=-VoucherLine.amount)), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code == ACC_ADVANCE,
                   Voucher.source_module == "claims",
                   or_(Voucher.source_ref.in_(pay_refs),
                       Voucher.source_ref.like(f"claim:advance-reclass:{care_month}:%")))) or 0)
        return net

    async def reclass_advance(self, session: AsyncSession, *, care_month: str,
                              on: date | None = None, actor: int | None = None) -> dict:
        """결정/삭감 변동 시 선수금(2141)을 목표(max(0, 지급합−인식AR))로 맞추는 양방향 델타 전기.

        과다지급으로 쌓인 선수금(공단)은 이후 인식 AR이 늘면(결정 도착) 상계(DR 2141/CR 1140)로 줄고,
        인식 AR이 줄면(재통보 추가삭감) 증액(DR 1140/CR 2141)해 1140이 상쇄 차변 없이 음수(대변잔액)로
        가는 것을 막는다. 통보서 인제스트가 결정 반영 후 호출. 델타 방식이라 멱등(잔액=목표면 no-op)·
        전기는 posting별 순번(vN) 유니크 ref(월 고정키 멱등충돌로 증분 유실되는 #627 패턴 방지)·
        채번은 advisory lock으로 직렬화(동시 :vN 충돌 방지).
        """
        from erp.finance.models import Voucher
        decided = await self._expected_for_month(session, care_month)
        if decided is None:
            return {"reclassed": False, "reason": "결정 데이터 없음"}
        await self._lock_advance(session, care_month)   # 채번·읽기-쓰기 직렬화
        from erp.claims.models import ClaimPayment
        paid = float(await session.scalar(
            select(func.coalesce(func.sum(ClaimPayment.paid_amt), 0))
            .where(ClaimPayment.care_month == care_month)) or 0)
        # 목표 선수금 = 지급합 − 인식 AR. MEDRLT 결정액이 아닌 제출청구 기준(1140 재현)으로 산정해
        # 결정액 상향(재통보) 시 1140을 상쇄 차변 없이 과대 대변하던 결함 차단. 미연계 시 결정액 폴백.
        ar_basis = await self._recognized_ar(session, care_month)
        if ar_basis is None:
            ar_basis = decided
        target = max(0, round(paid - ar_basis))
        current = round(await self._advance_balance(session, care_month))
        delta = current - target
        if delta == 0:
            return {"reclassed": False, "advance": current}
        seq = int(await session.scalar(
            select(func.count(Voucher.id)).where(
                Voucher.source_module == "claims",
                Voucher.source_ref.like(f"claim:advance-reclass:{care_month}:v%"))) or 0)
        if delta > 0:       # 인식 AR 증가 → 선수금 상계(2141↓)
            lines = [VoucherLineDTO(ACC_ADVANCE, "DEBIT", delta),
                     VoucherLineDTO(ACC_RECEIVABLE, "CREDIT", delta)]
            memo = f"선수금(공단) 상계 {care_month} — 결정 도착분"
        else:               # 인식 AR 감소(추가삭감) → 선수금 증액(2141↑·1140 음수화 방지)
            lines = [VoucherLineDTO(ACC_RECEIVABLE, "DEBIT", -delta),
                     VoucherLineDTO(ACC_ADVANCE, "CREDIT", -delta)]
            memo = f"선수금(공단) 증액 {care_month} — 추가 삭감분"
        v = await self._vouchers.create(
            session, source_module="claims",
            source_ref=f"claim:advance-reclass:{care_month}:v{seq + 1}",
            voucher_on=on or date.today(), lines=lines, memo=memo, created_by=actor)
        return {"reclassed": True, "voucher_id": v.id, "amount": delta,
                "advance": current - delta}

    async def reconciliation(self, session: AsyncSession, *, care_month: str) -> dict:
        """진료월 청구·결정·지급 대사 요약 — 미수(잔여)=결정−지급, 삭감=청구−결정, 선수(과다지급)."""
        from erp.claims.models import ClaimBatch, ClaimPayment, ClaimStatement
        claimed, decided = (await session.execute(
            select(func.coalesce(func.sum(ClaimStatement.claimed_amt), 0),
                   func.coalesce(func.sum(ClaimStatement.decided_amt), 0))
            .join(ClaimBatch, ClaimBatch.id == ClaimStatement.batch_id)
            .where(ClaimBatch.care_month == care_month))).one()
        paid = await session.scalar(
            select(func.coalesce(func.sum(ClaimPayment.paid_amt), 0))
            .where(ClaimPayment.care_month == care_month))
        claimed, decided, paid = float(claimed), float(decided), float(paid or 0)
        # 2026-10-09 시나리오 S-B: 이의 인정 복원분을 빼고 대사해 GL은 맞는데 화면은 선수금·차이를 보였다 —
        # 지급 기대액 = 결정액 + 인정 복원액(record·인식 AR과 같은 기준). decided는 MEDRLT 원값 유지.
        restored = await self._appeal_restored(session, care_month)
        expected = decided + restored
        status = ("DECIDED" if paid <= 0
                  else "PAID" if expected > 0 and paid >= expected else "PARTIALLY_PAID")
        return {"care_month": care_month, "claimed": round(claimed), "decided": round(decided),
                "appeal_restored": round(restored), "expected": round(expected),
                "reduced": round(claimed - decided),            # 통보서 삭감(화면 '청구−결정' 정의 유지)
                "paid": round(paid),
                "outstanding": max(0, round(expected - paid)),  # 공단 미지급 잔여(기대−지급)
                "advance": max(0, round(paid - expected)),      # 과다지급 선수금(2141) 대응분
                "status": status}

    async def list_payments(self, session: AsyncSession, *, care_month: str | None = None,
                            limit: int = 200) -> list[dict]:
        from erp.claims.models import ClaimPayment
        q = select(ClaimPayment).order_by(ClaimPayment.paid_on.desc(), ClaimPayment.id.desc())
        if care_month:
            q = q.where(ClaimPayment.care_month == care_month)
        rows = (await session.scalars(q.limit(limit))).all()
        return [{"id": p.id, "payment_ref": p.payment_ref, "care_month": p.care_month,
                 "paid_on": p.paid_on, "paid_amt": float(p.paid_amt),
                 "expected_amt": float(p.expected_amt) if p.expected_amt is not None else None,
                 "variance": float(p.variance) if p.variance is not None else None,
                 "voucher_ref": p.voucher_ref, "note": p.note} for p in rows]


class ClaimResubmissionService:
    """반송(반려) 청구 재청구 추적 — 이의신청(사후 삭감분쟁)과 별개.

    심사 전 반송 청구의 정정·재제출을 추적해 미재청구(RETURNED) 누락에 의한 수익 유실을 방지.
    운영 추적 — 회계 전기 없음(최종 결정은 기존 claim.reviewed).
    """

    async def record_return(self, session: AsyncSession, *, actor: int, original_ref: str,
                            care_month: str, returned_on: date, return_reason: str | None = None,
                            return_code: str | None = None, amount: float | None = None,
                            note: str | None = None):
        """반송 접수 — 원 청구(original_ref) 멱등. 이미 있으면 기존 반환."""
        from erp.claims.models import ClaimResubmission
        original_ref = (original_ref or "").strip()   # 진입점 1회 정규화(dedup·저장 동일형)
        if not original_ref:
            raise ValidationError("원 청구 식별(original_ref)은 필수입니다")
        care_month = _require_month(care_month)
        if amount is not None and amount < 0:
            raise ValidationError("반송 금액은 음수일 수 없습니다")
        existing = (await session.execute(
            select(ClaimResubmission).where(
                ClaimResubmission.original_ref == original_ref))).scalar_one_or_none()
        if existing is not None:
            return existing                   # 멱등 — 동일 반송 재접수 안전
        r = ClaimResubmission(
            original_ref=original_ref, care_month=care_month, returned_on=returned_on,
            return_reason=(return_reason or "").strip() or None,
            return_code=(return_code or "").strip() or None,
            amount=(round(amount) if amount is not None else None),
            note=(note or "").strip() or None, status="RETURNED", created_by=actor)
        session.add(r)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="claims.claim_resubmissions", target_id=str(r.id),
                                  after={"original_ref": original_ref, "care_month": care_month})
        return r

    async def resubmit(self, session: AsyncSession, *, actor: int, resubmission_id: int,
                       resubmit_ref: str, resubmitted_on: date):
        """재청구 연결 — RETURNED→RESUBMITTED. 원 반송에 재청구 접수번호·일자 기록."""
        from erp.claims.models import ClaimResubmission
        r = await session.get(ClaimResubmission, resubmission_id)
        if r is None:
            raise ValidationError("존재하지 않는 반송 건입니다")
        if r.status != "RETURNED":
            raise ValidationError(f"재청구 가능 상태가 아닙니다 ({r.status})")
        if not (resubmit_ref or "").strip():
            raise ValidationError("재청구 접수번호(resubmit_ref)는 필수입니다")
        r.resubmit_ref = resubmit_ref.strip()
        r.resubmitted_on = resubmitted_on
        r.status = "RESUBMITTED"
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="claims.claim_resubmissions", target_id=str(r.id),
                                  after={"status": "RESUBMITTED", "resubmit_ref": r.resubmit_ref})
        return r

    async def close(self, session: AsyncSession, *, actor: int, resubmission_id: int,
                    note: str | None = None):
        """종결 — 재청구 결정 반영·포기 등으로 추적 종료(RETURNED/RESUBMITTED→CLOSED)."""
        from erp.claims.models import ClaimResubmission
        r = await session.get(ClaimResubmission, resubmission_id)
        if r is None:
            raise ValidationError("존재하지 않는 반송 건입니다")
        r.status = "CLOSED"
        if note:
            r.note = note.strip()[:2000] or r.note
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="claims.claim_resubmissions", target_id=str(r.id),
                                  after={"status": "CLOSED"})
        return r

    async def list_resubmissions(self, session: AsyncSession, *, status: str | None = None,
                                 pending: bool = False, care_month: str | None = None,
                                 limit: int = 200) -> dict:
        """반송/재청구 목록 — pending=미재청구(RETURNED)만. 미재청구 총액 요약."""
        from erp.claims.models import ClaimResubmission
        q = select(ClaimResubmission).order_by(ClaimResubmission.returned_on.desc(),
                                               ClaimResubmission.id.desc())
        if pending:
            q = q.where(ClaimResubmission.status == "RETURNED")
        elif status:
            q = q.where(ClaimResubmission.status == status)
        if care_month:
            q = q.where(ClaimResubmission.care_month == care_month)
        rows = (await session.scalars(q.limit(limit))).all()
        pending_amt = sum(float(r.amount or 0) for r in rows if r.status == "RETURNED")
        return {"items": [{"id": r.id, "original_ref": r.original_ref,
                           "care_month": r.care_month, "returned_on": r.returned_on,
                           "return_code": r.return_code, "return_reason": r.return_reason,
                           "amount": float(r.amount) if r.amount is not None else None,
                           "resubmit_ref": r.resubmit_ref, "resubmitted_on": r.resubmitted_on,
                           "status": r.status} for r in rows],
                "pending_amount": round(pending_amt)}


class ClaimSubmissionService:
    """청구액 이중소스 정합 — 제출(claim.submitted 캡처) vs 심사대상(MEDRLT ClaimStatement) 대사.

    두 소스가 독립 기록되어 불일치(제출 누락·심사결과 누락·금액차)를 못 잡던 갭 해소. 진료월 단위
    제출 청구액과 심사대상 청구액을 비교하고, 진료월 미동봉(care_month NULL) 제출은 미대사로 표면화.
    """

    async def reconciliation(self, session: AsyncSession, *, care_month: str) -> dict:
        """진료월 제출액 vs 심사대상 청구액 대사 — variance≠0이면 불일치(누락·금액차)."""
        from erp.claims.models import ClaimBatch, ClaimStatement, ClaimSubmission
        sub_cnt, submitted = (await session.execute(
            select(func.count(ClaimSubmission.id),
                   func.coalesce(func.sum(ClaimSubmission.claimed_amt), 0))
            .where(ClaimSubmission.care_month == care_month))).one()
        stmt_cnt, reviewed = (await session.execute(
            select(func.count(ClaimStatement.id),
                   func.coalesce(func.sum(ClaimStatement.claimed_amt), 0))
            .join(ClaimBatch, ClaimBatch.id == ClaimStatement.batch_id)
            .where(ClaimBatch.care_month == care_month))).one()
        submitted, reviewed = float(submitted), float(reviewed)
        variance = round(submitted - reviewed)
        return {"care_month": care_month, "submitted": round(submitted),
                "reviewed_claimed": round(reviewed), "variance": variance,
                "matched": variance == 0, "submission_count": int(sub_cnt),
                "statement_count": int(stmt_cnt)}

    async def unlinked_submissions(self, session: AsyncSession, *, limit: int = 200) -> dict:
        """진료월 미동봉(care_month NULL) 제출 청구 — 대사 불가분 표면화(HIS careMonth 요청 근거)."""
        from erp.claims.models import ClaimSubmission
        rows = (await session.scalars(
            select(ClaimSubmission).where(ClaimSubmission.care_month.is_(None))
            .order_by(ClaimSubmission.id.desc()).limit(limit))).all()
        total = sum(float(r.claimed_amt or 0) for r in rows)
        return {"items": [{"id": r.id, "claim_id": r.claim_id, "claim_number": r.claim_number,
                           "claimed_amt": float(r.claimed_amt), "submitted_on": r.submitted_on}
                          for r in rows], "count": len(rows), "amount": round(total)}
