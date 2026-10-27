"""공통 Excel 내보내기 (FR-22) — design §4.3 `export.xlsx`, openpyxl write_only (§11.0).

원칙: 권한 + 행 scope 합성 + EXPORT 접속기록 + 목록 조회 재사용 (§6.2).
5천 행 초과 시 ValueError → 비동기 잡 전환은 운영 진입 시.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.excel import SYNC_ROW_LIMIT, build_xlsx
from erp.common.policy import ScopeKind, require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.errors import ForbiddenError, ValidationError

router = APIRouter(tags=["export"])
_audit = AuditService()

# 리소스 → (정책 리소스, 조회 함수) — 목록 서비스 재사용 원칙
EXPORTABLE = {"employees", "items", "vouchers"}


@router.get("/export/{resource}.xlsx")
async def export_resource(resource: str,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    if resource not in EXPORTABLE:
        raise ValidationError("내보내기를 지원하지 않는 리소스입니다", {"resource": resource})

    if resource == "employees":
        scope = require_scope(user, "hr.employees", "read")
        if scope.kind == ScopeKind.SELF:        # 본인만 권한이면 전체 반출 불가
            raise ForbiddenError()
        from erp.hr.models import Employee
        q = select(Employee).order_by(Employee.id)
        if scope.kind == ScopeKind.DEPT:  # 행 scope 명시 합성 — export도 동일 (§6.2)
            q = q.where(Employee.department_id == scope.dept_id)
        # 적재 상한 — build_xlsx(SYNC_ROW_LIMIT) 가드보다 먼저 메모리 폭주 방지(+1로 초과 감지)
        rows = (await session.execute(q.limit(SYNC_ROW_LIMIT + 1))).scalars().all()
        header = ["사번", "이름", "부서ID", "직급", "입사일"]
        data = [[e.employee_no, e.name, e.department_id, e.position, e.hired_on]
                for e in rows]
    elif resource == "items":
        require_scope(user, "scm.items", "read")     # 분석 H2 — 누락이던 권한 검증
        from erp.scm.models import Item
        rows = (await session.execute(
            select(Item).order_by(Item.id).limit(SYNC_ROW_LIMIT + 1))).scalars().all()
        header = ["코드", "품명", "분류", "마약류", "안전재고"]
        data = [[i.code, i.name, i.category, i.is_narcotic, float(i.safety_stock_qty)]
                for i in rows]
    else:  # vouchers
        require_scope(user, "finance.vouchers", "read")
        from erp.finance.models import Voucher
        rows = (await session.execute(
            select(Voucher).order_by(Voucher.id).limit(SYNC_ROW_LIMIT + 1))).scalars().all()
        header = ["ID", "일자", "출처", "참조", "상태", "적요"]
        data = [[v.id, v.voucher_on, v.source_module, v.source_ref, v.status, v.memo]
                for v in rows]

    # 반출 접속기록 — 법정 (NFR Audit ④)
    await _audit.record_access(session, actor=user.user_id, action="EXPORT",
                               entity=resource, route=f"/export/{resource}.xlsx")

    content = build_xlsx(sheet_name=resource, headers=header, rows=data)
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{resource}.xlsx"'})
