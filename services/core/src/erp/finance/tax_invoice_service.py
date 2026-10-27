"""전자세금계산서 수취·대사 (A-1) — 등록(수기·홈택스 CSV)·미지급금 대사·증빙 현황.

대사 규칙: 합계금액 == 미지급금 잔액(amount−paid) 또는 전액(amount) 일치 시 매칭.
불일치는 명시 오류(증빙-채무 어긋남 가시화). 승인번호 UNIQUE 멱등.
"""
import csv
import io
from datetime import date, datetime

from sqlalchemy import String as SAString
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.finance.models import Payable, TaxInvoice

_audit = AuditService()

# 홈택스 매입 세금계산서 목록 CSV 열(다운로드 양식 가정 — 헤더명 기준 유연 매핑)
_CSV_COLS = {"승인번호": "approval_no", "작성일자": "issue_date",
             "공급자사업자등록번호": "supplier_bizno", "상호": "supplier_name",
             "공급자상호": "supplier_name", "공급가액": "supply_amount",
             "세액": "vat_amount", "합계금액": "total_amount", "종류": "invoice_type"}


def _num(v: str | float | None) -> float:
    if v in (None, ""):
        return 0.0
    return float(str(v).replace(",", ""))


class TaxInvoiceService:
    async def register(self, session: AsyncSession, *, actor: int, approval_no: str,
                       issue_date: date, supplier_bizno: str, supply_amount: float,
                       vat_amount: float = 0, supplier_name: str | None = None,
                       invoice_type: str = "TAX", direction: str = "IN",
                       total_amount: float | None = None,
                       note: str | None = None) -> TaxInvoice:
        approval_no = approval_no.strip()
        if not approval_no:
            raise ValidationError("승인번호는 필수입니다")
        existing = await session.scalar(select(TaxInvoice).where(
            TaxInvoice.approval_no == approval_no))
        if existing is not None:
            return existing                      # 승인번호 멱등 — 재업로드 안전
        if invoice_type == "EXEMPT" and vat_amount:
            raise ValidationError("계산서(면세)는 세액이 0이어야 합니다")
        total = total_amount if total_amount is not None else supply_amount + vat_amount
        if round(total) != round(supply_amount + vat_amount):
            raise ValidationError("합계 ≠ 공급가액+세액 — 원본 확인 필요")
        inv = TaxInvoice(approval_no=approval_no, invoice_type=invoice_type,
                         direction=direction, issue_date=issue_date,
                         supplier_bizno=supplier_bizno.strip(),
                         supplier_name=supplier_name, supply_amount=supply_amount,
                         vat_amount=vat_amount, total_amount=total,
                         note=note, created_by=actor)
        session.add(inv)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="finance.tax_invoices", target_id=str(inv.id),
                                  after={"approval_no": approval_no, "total": total})
        return inv

    async def upload_csv(self, session: AsyncSession, *, actor: int,
                         content: bytes) -> dict:
        """홈택스 매입 목록 CSV — 헤더명 매핑, 행별 격리, 승인번호 멱등."""
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        created = skipped = failed = 0
        errors: list[str] = []
        for i, raw in enumerate(reader, start=2):
            if i > 5002:                         # M-2: 행수 상한 5,000(헤더 제외)
                errors.append("5,000행 초과분 미처리 — 분할 업로드하세요")
                break
            row = {}
            for k, v in raw.items():
                key = _CSV_COLS.get((k or "").strip())
                if key:
                    row[key] = (v or "").strip()
            try:
                if not row.get("approval_no"):
                    raise ValidationError("승인번호 없음")
                itype = row.get("invoice_type") or "TAX"
                if "계산서" in itype and "세금" not in itype:
                    itype = "EXEMPT"
                elif itype not in ("TAX", "EXEMPT", "ZERO"):
                    itype = "TAX"
                before = await session.scalar(select(func.count(TaxInvoice.id)).where(
                    TaxInvoice.approval_no == row["approval_no"]))
                inv = await self.register(
                    session, actor=actor, approval_no=row["approval_no"],
                    issue_date=datetime.strptime(
                        row.get("issue_date", "").replace(".", "-").replace("/", "-"),
                        "%Y-%m-%d").date(),
                    supplier_bizno=row.get("supplier_bizno", ""),
                    supplier_name=row.get("supplier_name"),
                    supply_amount=_num(row.get("supply_amount")),
                    vat_amount=_num(row.get("vat_amount")),
                    total_amount=_num(row.get("total_amount")) or None,
                    invoice_type=itype)
                if before:
                    skipped += 1
                else:
                    created += 1
                _ = inv
            except Exception as exc:  # noqa: BLE001 — 행별 격리
                failed += 1
                if len(errors) < 5:
                    errors.append(f"{i}행: {str(exc)[:80]}")
        return {"created": created, "duplicated": skipped, "failed": failed,
                "errors": errors}

    async def match(self, session: AsyncSession, *, actor: int, invoice_id: int,
                    payable_id: int) -> TaxInvoice:
        """미지급금 대사 — 합계금액과 채무 금액(전액 또는 잔액) 일치 검증."""
        inv = await session.get(TaxInvoice, invoice_id)
        if inv is None:
            raise ValidationError("존재하지 않는 세금계산서입니다")
        if inv.status == "MATCHED":
            raise ValidationError("이미 대사된 건입니다")
        ap = await session.get(Payable, payable_id)
        if ap is None:
            raise ValidationError("존재하지 않는 미지급금입니다")
        remaining = float(ap.amount) - float(ap.paid_amount)
        if round(float(inv.total_amount)) not in (round(float(ap.amount)), round(remaining)):
            raise ValidationError(
                f"금액 불일치 — 계산서 {float(inv.total_amount):,.0f} vs "
                f"채무 {float(ap.amount):,.0f}(잔액 {remaining:,.0f})")
        inv.status, inv.payable_id = "MATCHED", payable_id
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="finance.tax_invoices", target_id=str(invoice_id),
                                  after={"matched_payable": payable_id})
        return inv

    async def unmatch(self, session: AsyncSession, *, actor: int,
                      invoice_id: int) -> TaxInvoice:
        inv = await session.get(TaxInvoice, invoice_id)
        if inv is None or inv.status != "MATCHED":
            raise ValidationError("대사 상태가 아닙니다")
        before = {"payable_id": inv.payable_id}
        inv.status, inv.payable_id = "RECEIVED", None
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="finance.tax_invoices", target_id=str(invoice_id),
                                  before=before, after={"unmatched": True})
        return inv

    async def list(self, session: AsyncSession, *, status: str | None = None,
                   month: str | None = None, page: int = 1, size: int = 50) -> dict:
        base = select(TaxInvoice)
        if status:
            base = base.where(TaxInvoice.status == status)
        if month:                                # YYYY-MM
            y, m = int(month[:4]), int(month[5:7])
            nxt = date(y + (m == 12), (m % 12) + 1, 1)
            base = base.where(TaxInvoice.issue_date >= date(y, m, 1),
                              TaxInvoice.issue_date < nxt)
        total = await session.scalar(
            select(func.count()).select_from(base.subquery())) or 0
        rows = (await session.scalars(
            base.order_by(TaxInvoice.issue_date.desc(), TaxInvoice.id.desc())
            .offset((page - 1) * size).limit(size))).all()
        items = [{"id": t.id, "approval_no": t.approval_no, "invoice_type": t.invoice_type,
                  "issue_date": t.issue_date, "supplier_bizno": t.supplier_bizno,
                  "supplier_name": t.supplier_name, "supply_amount": float(t.supply_amount),
                  "vat_amount": float(t.vat_amount), "total_amount": float(t.total_amount),
                  "status": t.status, "payable_id": t.payable_id}
                 for t in rows]
        return {"items": items, "total": total, "page": page, "size": size}

    async def summary(self, session: AsyncSession, *, year: int) -> dict:
        """연/월별 과세·면세 매입 집계 — 사업장현황신고·부가세(A-2) 기초 + 미대사 현황."""
        ym_col = func.substr(cast(TaxInvoice.issue_date, SAString), 1, 7)
        rows = (await session.execute(
            select(ym_col, TaxInvoice.invoice_type,
                   func.count(TaxInvoice.id), func.sum(TaxInvoice.supply_amount),
                   func.sum(TaxInvoice.vat_amount))
            .where(TaxInvoice.issue_date >= date(year, 1, 1),
                   TaxInvoice.issue_date <= date(year, 12, 31),
                   TaxInvoice.status != "VOID")
            .group_by(ym_col, TaxInvoice.invoice_type))).all()
        unmatched = (await session.execute(
            select(func.count(TaxInvoice.id), func.coalesce(func.sum(
                TaxInvoice.total_amount), 0))
            .where(TaxInvoice.status == "RECEIVED"))).one()
        return {"year": year,
                "months": [{"month": ym, "invoice_type": it, "count": int(n),
                            "supply_amount": float(s or 0), "vat_amount": float(v or 0)}
                           for ym, it, n, s, v in rows],
                "unmatched": {"count": int(unmatched[0]),
                              "total_amount": float(unmatched[1])}}
