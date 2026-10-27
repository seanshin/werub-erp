"""환자 민원·CS(VOC) — 접수→분류→배정→처리→회신→종결 (Tier3 ①·마이그 0223).

의료기관 인증평가(민원·고충처리 체계) 소스. 처리기한 = 접수 + 14일(민원처리법 준용 내부 SLA).
전이는 행잠금 + voc_actions 이력 + 감사로그. 연락처는 AES-GCM(환자 연결 시 미저장 — 최소수집).
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common import crypto
from erp.common.doc_no import lock_doc_no
from erp.errors import ForbiddenError, ValidationError
from erp.patient.models import Patient, VocAction, VocCase

_audit = AuditService()
_ENT = "patient.voc_cases"
SLA_DAYS = 14


class VocService:
    async def _next_no(self, session: AsyncSession, year: int) -> str:
        await lock_doc_no(session, f"민원-{year}")
        n = int(await session.scalar(
            select(func.count()).select_from(VocCase).where(
                VocCase.case_no.like(f"민원-{year}-%"))) or 0) + 1
        return f"민원-{year}-{n:04d}"

    async def receive(self, session: AsyncSession, *, created_by: int, title: str,
                      body: str | None = None, channel: str = "DESK",
                      vtype: str = "COMPLAINT", category: str = "ETC", severity: str = "LOW",
                      patient_id: int | None = None, contact: str | None = None,
                      department_id: int | None = None, on: date | None = None) -> VocCase:
        if not (title or "").strip():
            raise ValidationError("제목은 필수입니다")
        if patient_id is not None and await session.get(Patient, patient_id) is None:
            raise ValidationError("존재하지 않는 환자입니다", {"patient_id": patient_id})
        on = on or date.today()
        case = VocCase(
            case_no=await self._next_no(session, on.year), channel=channel.upper(),
            vtype=vtype.upper(), category=category.upper(), severity=severity.upper(),
            patient_id=patient_id, title=title[:200], body=body,
            department_id=department_id, received_on=on, due_on=on + timedelta(days=SLA_DAYS),
            created_by=created_by)
        # 최소수집 — 환자 연결이 없을 때만 회신 연락처 저장(AES-GCM)
        if patient_id is None and (contact or "").strip():
            case.contact_enc = crypto.encrypt_column(contact.strip(), aad="voc.contact")
        session.add(case)
        await session.flush()
        await _audit.record_audit(session, actor=created_by, action="CREATE", entity=_ENT,
                                  target_id=str(case.id),
                                  after={"case_no": case.case_no, "vtype": case.vtype,
                                         "severity": case.severity})
        return case

    async def _get(self, session: AsyncSession, case_id: int, *, lock: bool = False) -> VocCase:
        c = await session.get(VocCase, case_id, with_for_update=lock)
        if c is None:
            raise ValidationError("존재하지 않는 민원입니다", {"case_id": case_id})
        return c

    def _log(self, session: AsyncSession, case: VocCase, action: str, actor: int,
             note: str | None = None) -> None:
        session.add(VocAction(case_id=case.id, action=action, actor=actor,
                              note=(note or "")[:500] or None))

    async def assign(self, session: AsyncSession, *, case_id: int, actor: int,
                     assignee_user_id: int, department_id: int | None = None,
                     note: str | None = None) -> VocCase:
        c = await self._get(session, case_id, lock=True)
        if c.status == "CLOSED":
            raise ValidationError("종결된 민원입니다 — 재오픈 후 처리하세요", {"status": c.status})
        c.assignee_user_id = assignee_user_id
        if department_id is not None:
            c.department_id = department_id
        if c.status == "RECEIVED":
            c.status = "IN_PROGRESS"
        self._log(session, c, "ASSIGN", actor, note or f"담당 지정 user:{assignee_user_id}")
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity=_ENT,
                                  target_id=str(c.id),
                                  after={"assignee": assignee_user_id, "status": c.status})
        return c

    async def comment(self, session: AsyncSession, *, case_id: int, actor: int,
                      note: str) -> VocCase:
        if not (note or "").strip():
            raise ValidationError("의견 내용이 필요합니다")
        c = await self._get(session, case_id)
        if c.status == "CLOSED":
            raise ValidationError("종결된 민원입니다", {"status": c.status})
        self._log(session, c, "COMMENT", actor, note)
        return c

    async def answer(self, session: AsyncSession, *, case_id: int, actor: int,
                     answer_body: str) -> VocCase:
        """회신 기록 — 처리 결과를 민원인에게 안내한 내용(전화/서면 요지 포함)."""
        if not (answer_body or "").strip():
            raise ValidationError("회신 내용이 필요합니다")
        c = await self._get(session, case_id, lock=True)
        if c.status not in ("RECEIVED", "IN_PROGRESS"):
            raise ValidationError("회신 가능한 상태가 아닙니다", {"status": c.status})
        c.status, c.answer_body = "ANSWERED", answer_body
        c.answered_at = datetime.now(timezone.utc)
        self._log(session, c, "ANSWER", actor)
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity=_ENT,
                                  target_id=str(c.id), after={"status": "ANSWERED"})
        return c

    async def close(self, session: AsyncSession, *, case_id: int, actor: int,
                    note: str | None = None) -> VocCase:
        c = await self._get(session, case_id, lock=True)
        if c.status != "ANSWERED":
            raise ValidationError("회신 후에만 종결할 수 있습니다", {"status": c.status})
        c.status, c.closed_at = "CLOSED", datetime.now(timezone.utc)
        self._log(session, c, "CLOSE", actor, note)
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity=_ENT,
                                  target_id=str(c.id), after={"status": "CLOSED"})
        return c

    async def reopen(self, session: AsyncSession, *, case_id: int, actor: int,
                     note: str) -> VocCase:
        if not (note or "").strip():
            raise ValidationError("재오픈 사유가 필요합니다")
        c = await self._get(session, case_id, lock=True)
        if c.status != "CLOSED":
            raise ValidationError("종결된 민원만 재오픈할 수 있습니다", {"status": c.status})
        c.status, c.reopen_count = "IN_PROGRESS", c.reopen_count + 1
        c.closed_at = None
        self._log(session, c, "REOPEN", actor, note)
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity=_ENT,
                                  target_id=str(c.id),
                                  after={"status": "IN_PROGRESS", "reopen": c.reopen_count})
        return c

    # ── 조회 (행스코프는 라우터에서 합성) ─────────────────────
    def _dict(self, c: VocCase, *, with_contact: bool = False) -> dict:
        out = {"id": c.id, "case_no": c.case_no, "channel": c.channel, "vtype": c.vtype,
               "category": c.category, "severity": c.severity, "patient_id": c.patient_id,
               "title": c.title, "department_id": c.department_id,
               "assignee_user_id": c.assignee_user_id, "status": c.status,
               "received_on": c.received_on, "due_on": c.due_on,
               "answered_at": c.answered_at, "closed_at": c.closed_at,
               "reopen_count": c.reopen_count, "safety_incident_id": c.safety_incident_id,
               "created_by": c.created_by,
               "overdue": c.status not in ("CLOSED",) and c.due_on < date.today()}
        if with_contact and c.contact_enc:
            try:
                out["contact"] = crypto.decrypt_column(c.contact_enc, aad="voc.contact")
            except Exception:  # noqa: BLE001 — 키 회전 등 복호 실패 시 마스킹
                out["contact"] = "(복호화 불가)"
        return out

    async def list_cases(self, session: AsyncSession, *, status: str | None = None,
                         vtype: str | None = None, category: str | None = None,
                         department_id: int | None = None, created_by: int | None = None,
                         overdue: bool = False) -> list[dict]:
        q = select(VocCase)
        if status:
            q = q.where(VocCase.status == status)
        if vtype:
            q = q.where(VocCase.vtype == vtype)
        if category:
            q = q.where(VocCase.category == category)
        if department_id is not None:
            q = q.where(VocCase.department_id == department_id)
        if created_by is not None:
            q = q.where(VocCase.created_by == created_by)
        if overdue:
            q = q.where(VocCase.status != "CLOSED", VocCase.due_on < date.today())
        rows = (await session.scalars(q.order_by(VocCase.id.desc()).limit(500))).all()
        return [self._dict(c) for c in rows]

    async def get_detail(self, session: AsyncSession, case_id: int) -> dict:
        c = await self._get(session, case_id)
        acts = (await session.scalars(select(VocAction).where(VocAction.case_id == case_id)
                                      .order_by(VocAction.id))).all()
        out = self._dict(c, with_contact=True)
        out["body"], out["answer_body"] = c.body, c.answer_body
        out["actions"] = [{"action": a.action, "actor": a.actor, "note": a.note, "at": a.at}
                          for a in acts]
        return out

    async def assert_row_scope(self, session: AsyncSession, c_or_id, *, scope,
                               user) -> VocCase:
        """행 스코프 — SELF=본인 접수분·DEPT=귀속부서(카드 EP 스코프 소거 결함 클래스 방지)."""
        from erp.common.policy import ScopeKind
        c = c_or_id if isinstance(c_or_id, VocCase) else await self._get(session, c_or_id)
        if scope.kind == ScopeKind.SELF and c.created_by != user.user_id:
            raise ForbiddenError("본인이 접수한 민원만 접근할 수 있습니다")
        if scope.kind == ScopeKind.DEPT and c.department_id != user.dept_id:
            raise ForbiddenError("본인 부서 귀속 민원만 접근할 수 있습니다")
        return c

    async def stats(self, session: AsyncSession, *, since: date, until: date) -> dict:
        """기간 통계 — 유형/카테고리/부서별 건수·평균 처리일수·기한초과(인증평가 자료 소스)."""
        rows = (await session.scalars(select(VocCase).where(
            VocCase.received_on >= since, VocCase.received_on <= until))).all()
        by_vtype: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_dept: dict[str, int] = {}
        closed_days, overdue, open_cnt = [], 0, 0
        today = date.today()
        for c in rows:
            by_vtype[c.vtype] = by_vtype.get(c.vtype, 0) + 1
            by_category[c.category] = by_category.get(c.category, 0) + 1
            dk = str(c.department_id) if c.department_id is not None else "미지정"
            by_dept[dk] = by_dept.get(dk, 0) + 1
            if c.status == "CLOSED" and c.closed_at is not None:
                closed_days.append((c.closed_at.date() - c.received_on).days)
            else:
                open_cnt += 1
                if c.due_on < today:
                    overdue += 1
        return {"since": since, "until": until, "total": len(rows),
                "open": open_cnt, "overdue": overdue,
                "avg_close_days": round(sum(closed_days) / len(closed_days), 1)
                if closed_days else None,
                "by_vtype": by_vtype, "by_category": by_category, "by_department": by_dept}
