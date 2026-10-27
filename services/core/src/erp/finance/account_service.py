"""계정과목(COA) 마스터 관리 — 생성·수정·비활성. 재무제표/대시보드 분류의 기반.

전표 라인은 account_code 문자열 참조(FK 아님)이라, 사용 중 코드의 변경/하드삭제는
과거 집계 탈락을 유발 → code는 생성 후 불변, 삭제는 비활성(active=false)로만.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import Account, VoucherLine

KINDS = ("ASSET", "LIABILITY", "CAPITAL", "REVENUE", "EXPENSE")
COST_TYPES = ("FIXED", "VARIABLE")
COST_FUNCTIONS = ("COGS", "SGA", "OTHER")
CF_ACTIVITIES = ("OPERATING", "INVESTING", "FINANCING")   # 현금흐름 활동(G8)
_KIND_KR = {"ASSET": "자산", "LIABILITY": "부채", "CAPITAL": "자본",
            "REVENUE": "수익", "EXPENSE": "비용"}


def _dict(a: Account, *, in_use: bool | None = None) -> dict:
    d = {"id": a.id, "code": a.code, "name": a.name, "kind": a.kind,
         "kind_kr": _KIND_KR.get(a.kind, a.kind), "parent_id": a.parent_id,
         "active": a.active, "cost_type": a.cost_type, "cost_function": a.cost_function,
         "is_current": a.is_current, "cf_activity": a.cf_activity,
         "ifrs_element": a.ifrs_element}
    if in_use is not None:
        d["in_use"] = in_use
    return d


class AccountService:
    async def list(self, session: AsyncSession, *, kind: str | None = None,
                   cost_type: str | None = None, include_inactive: bool = True) -> list[dict]:
        q = select(Account).order_by(Account.kind, Account.code)
        if kind:
            q = q.where(Account.kind == kind)
        if cost_type:
            q = q.where(Account.cost_type == cost_type)
        if not include_inactive:
            q = q.where(Account.active.is_(True))
        rows = (await session.scalars(q)).all()
        # 사용 중(전표 라인 참조) 코드 — code 변경/비활성 시 경고·차단 근거
        used = {c for (c,) in (await session.execute(
            select(VoucherLine.account_code).distinct()))}
        return [_dict(a, in_use=a.code in used) for a in rows]

    async def create(self, session: AsyncSession, *, code: str, name: str, kind: str,
                     parent_id: int | None = None, cost_type: str | None = None,
                     is_current: bool | None = None, cf_activity: str | None = None,
                     ifrs_element: str | None = None) -> Account:
        code = (code or "").strip()
        if not code or not name:
            raise ValidationError("코드·명칭은 필수입니다")
        if kind not in KINDS:
            raise ValidationError(f"계정 종류는 {'/'.join(KINDS)} 중 하나여야 합니다")
        if cost_type and cost_type not in COST_TYPES:
            raise ValidationError("원가구분은 FIXED/VARIABLE 입니다")
        if cf_activity and cf_activity not in CF_ACTIVITIES:
            raise ValidationError("현금흐름 활동은 OPERATING/INVESTING/FINANCING 입니다")
        if await session.scalar(select(func.count()).select_from(Account).where(Account.code == code)):
            raise ValidationError("이미 존재하는 계정 코드입니다", {"code": code})
        a = Account(code=code, name=name.strip(), kind=kind, parent_id=parent_id,
                    active=True, cost_type=cost_type or None,
                    is_current=is_current, cf_activity=cf_activity or None,
                    ifrs_element=(ifrs_element or "").strip() or None)
        session.add(a)
        await session.flush()
        return a

    async def update(self, session: AsyncSession, *, account_id: int, name: str | None = None,
                     kind: str | None = None, parent_id: int | None = None,
                     active: bool | None = None, cost_type: str | None = None,
                     clear_cost_type: bool = False, cost_function: str | None = None,
                     clear_cost_function: bool = False, is_current: bool | None = None,
                     clear_is_current: bool = False, cf_activity: str | None = None,
                     clear_cf_activity: bool = False, ifrs_element: str | None = None,
                     clear_ifrs_element: bool = False) -> Account:
        a = await session.get(Account, account_id)
        if a is None:
            raise ValidationError("계정과목을 찾을 수 없습니다")
        if clear_ifrs_element:
            a.ifrs_element = None
        elif ifrs_element is not None:
            a.ifrs_element = ifrs_element.strip() or None
        if clear_cost_function:
            a.cost_function = None
        elif cost_function is not None:
            if cost_function not in COST_FUNCTIONS:
                raise ValidationError("기능구분은 COGS/SGA/OTHER 입니다")
            a.cost_function = cost_function
        if clear_is_current:
            a.is_current = None
        elif is_current is not None:
            a.is_current = is_current
        if clear_cf_activity:
            a.cf_activity = None
        elif cf_activity is not None:
            if cf_activity not in CF_ACTIVITIES:
                raise ValidationError("현금흐름 활동은 OPERATING/INVESTING/FINANCING 입니다")
            a.cf_activity = cf_activity
        if kind is not None and kind != a.kind:
            if kind not in KINDS:
                raise ValidationError(f"계정 종류는 {'/'.join(KINDS)} 중 하나여야 합니다")
            # 전기 내역 있는 계정의 종류 변경 금지 — code는 FK 아닌 문자열 참조라 kind 변경 시
            # 과거 전 기간 재무제표가 소급 재분류(자산↔비용 등)되어 왜곡됨.
            used = await session.scalar(select(func.count()).select_from(VoucherLine)
                                        .where(VoucherLine.account_code == a.code))
            if used:
                raise ValidationError(
                    "전기 내역이 있는 계정의 종류는 변경할 수 없습니다(재무제표 소급 왜곡 방지)",
                    {"code": a.code, "lines": int(used)})
            a.kind = kind
        elif kind is not None and kind not in KINDS:
            raise ValidationError(f"계정 종류는 {'/'.join(KINDS)} 중 하나여야 합니다")
        if name is not None:
            a.name = name.strip()
        if parent_id is not None:
            a.parent_id = parent_id or None
        if active is not None:
            a.active = active
        if clear_cost_type:
            a.cost_type = None
        elif cost_type is not None:
            if cost_type not in COST_TYPES:
                raise ValidationError("원가구분은 FIXED/VARIABLE 입니다")
            a.cost_type = cost_type
        await session.flush()
        return a

    async def classification_coverage(self, session: AsyncSession) -> dict:
        """IFRS 분류(G8) 커버리지 — is_current/cf_activity 미지정(하드코딩 폴백 의존) 계정 감사.

        미지정이 곧 오류는 아니나(statements.py 폴백), 데이터 주도 완결도를 높이려면 채워야 한다.
        활성 계정 중 자산/부채(is_current)·재무상태표 계정(cf_activity) 미지정을 목록화."""
        accts = (await session.scalars(select(Account).where(Account.active.is_(True)))).all()
        miss_cur = sorted(a.code for a in accts
                          if a.kind in ("ASSET", "LIABILITY") and a.is_current is None)
        miss_cf = sorted(a.code for a in accts
                         if a.kind in ("ASSET", "LIABILITY", "CAPITAL") and a.cf_activity is None)
        # G10 element 태그 커버리지 — 전 활성계정 대상(XBRL 인스턴스 태깅 완결도)
        miss_elem = sorted(a.code for a in accts if a.ifrs_element is None)
        return {"missing_is_current": {"count": len(miss_cur), "codes": miss_cur},
                "missing_cf_activity": {"count": len(miss_cf), "codes": miss_cf},
                "missing_ifrs_element": {"count": len(miss_elem), "codes": miss_elem,
                                         "total": len(accts)}}

    async def statements_taxonomy(self, session: AsyncSession) -> dict:
        """개념 태깅 재무제표 매핑 export(G10 기초) — 계정별 IFRS element·BS/CF 분류.

        ⚠ DART XBRL 인스턴스가 아니라, 인스턴스 생성의 입력이 되는 계정↔concept 매핑표.
        규제 확인(xbrl-regulatory-decision.md) 후 인스턴스 생성기에서 재무제표 값과 결합한다."""
        accts = (await session.scalars(select(Account).where(Account.active.is_(True))
                                       .order_by(Account.code))).all()
        tagged = [a for a in accts if a.ifrs_element]
        return {"taxonomy_note": "best-effort ifrs-full concept — 감독기관 지정 택소노미 확정 시 교체",
                "tagged_count": len(tagged), "total": len(accts),
                "items": [{"code": a.code, "name": a.name, "kind": a.kind,
                           "ifrs_element": a.ifrs_element, "is_current": a.is_current,
                           "cf_activity": a.cf_activity} for a in tagged]}

    @staticmethod
    async def codes_by_cost_type(session: AsyncSession, cost_type: str) -> set[str]:
        """대시보드 원가 집계용 — 해당 cost_type(FIXED|VARIABLE) 계정 코드 집합."""
        return {c for (c,) in (await session.execute(
            select(Account.code).where(Account.cost_type == cost_type)))}

    @staticmethod
    async def fixed_cost_codes(session: AsyncSession) -> set[str]:
        """대시보드 고정비 집계용 — cost_type='FIXED' 계정 코드 집합."""
        return await AccountService.codes_by_cost_type(session, "FIXED")
