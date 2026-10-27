"""컴플라이언스 만기·한도 통합 (크로스도메인 집계) — 흩어진 법정 기한을 하나의 운영 뷰로.

여러 도메인에 흩어진 만기/한도 데이터(면허·법정교육/검진·4대보험 신고·방사선 장비검사·재고 유효기간·
위수탁/구매 계약·의료진 계약·계약직 근로계약·의료폐기물 보관기한 + 방사선 피폭한도·환자안전 미검토·
의약품 회수 미조치)를 as_of 기준 긴급도로 통합. app 계층(크로스도메인)에서 hr·scm·regulatory를 읽는다
(analytics는 규제 접근 계약상 금지).

읽기 전용 — 원천 데이터를 조회·분류만 하고 변경하지 않는다(각 도메인 서비스가 등록/처리 담당).
"""
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.contract_lifecycle import ACTIVE, EXPIRING
from erp.hr.models import (
    ComplianceRecord, Employee, InsuranceReportEvent, License, MedicalStaffContract,
)
from erp.itam.models import ITAsset, SoftwareService, VendorContract
from erp.regulatory.models import PatientSafetyIncident, RadiationDose, RadiationEquipment
from erp.scm.models import DrugRecall, Item, Lot, MedicalWasteLog, OutsourcingContract

_ITAM_LIVE = ("IN_STOCK", "ASSIGNED", "REPAIR", "LOANER")   # 보증 추적 대상(폐기·불용·분실 제외)

_LIVE_CONTRACT = (ACTIVE, EXPIRING)   # 만기 추적 대상 상태(만료·해지·초안 제외)
_RAD_ANNUAL_LIMIT_MSV = 50.0     # 진단용방사선 안전관리규칙 연 한도
_RAD_5Y_LIMIT_MSV = 100.0        # 5년 누적 한도(연 50과 병행 — 계산검토 M-7)
_RAD_NEAR_RATIO = 0.8            # 한도 80% 도달 시 주의
_LONG_OVERDUE_DAYS = 180         # 180일 초과 경과 = 장기 미해결(별도 분리 — 실행 뷰 오염 방지)
# 의료폐기물 법정 보관기간(폐기물관리법 시행규칙 별표5·냉장4℃ 기준 표준 — 운영 조건별 조정 가능).
# 상온 보관은 더 짧으므로 이 값은 보수적(조기 경고). 손상성 30일·격리 7일(계산검토 M-6 —
# 종전 기본 15일 처리로 격리폐기물 경고가 8일 지각하던 것 정정).
_WASTE_STORAGE_DAYS = {"손상성": 30, "격리": 7}
_WASTE_STORAGE_DEFAULT = 15      # 위해·일반·병리계 등


def _band(days: int) -> str:
    """긴급도 밴드 — 장기경과·경과(overdue)·D-7·D-30·D-90.

    long_overdue는 숨기지 않되(면허 만료 등 컴플라이언스 리스크는 보존) 실행 뷰에서 하단·별도 집계.
    """
    if days < -_LONG_OVERDUE_DAYS:
        return "long_overdue"
    if days < 0:
        return "overdue"
    if days <= 7:
        return "d7"
    if days <= 30:
        return "d30"
    return "d90"


class ComplianceDeadlineService:
    async def deadlines(self, session: AsyncSession, *, as_of: date,
                        horizon_days: int = 90) -> dict:
        """만기 임박·경과 항목 통합 — 기한형(items) + 한도/백로그형(alerts).

        items: {category, title, subject, due_on, days_remaining, band, status} — due_on 오름차순.
        summary: 밴드별·카테고리별 건수. alerts: 방사선 피폭 근접/초과·환자안전 미검토(비기한형).
        """
        cutoff = as_of + timedelta(days=horizon_days)
        emp_name = dict((await session.execute(select(Employee.id, Employee.name))).all())
        items: list[dict] = []

        def _add(category: str, title: str, subject: str | None, due_on: date, status: str) -> None:
            days = (due_on - as_of).days
            items.append({"category": category, "title": title, "subject": subject,
                          "due_on": str(due_on), "days_remaining": days,
                          "band": _band(days), "status": status})

        # ① 면허·자격 만료(hr.License) — 무기한(NULL) 제외
        for lic in (await session.execute(
                select(License).where(License.expires_on.isnot(None),
                                      License.expires_on <= cutoff)
                .order_by(License.expires_on))).scalars():
            _add("면허", f"{lic.license_type} {lic.license_no}",
                 emp_name.get(lic.employee_id), lic.expires_on, "유효")

        # ② 법정교육·건강검진 이수 만료(hr.ComplianceRecord)
        for cr in (await session.execute(
                select(ComplianceRecord).where(ComplianceRecord.valid_until <= cutoff)
                .order_by(ComplianceRecord.valid_until))).scalars():
            cat = "법정교육" if cr.kind == "TRAINING" else "건강검진"
            _add(cat, cr.category, emp_name.get(cr.employee_id), cr.valid_until, "이수")

        # ②-b 원내 필수교육 미이수/만료(hr.training·P1-2) — 신규 교육이수 도메인 편입
        from erp.hr.training_service import TrainingService
        tstatus = await TrainingService().status(session, as_of=as_of)
        course_title = {c["code"]: c["title"] for c in tstatus["courses"]}
        for it in tstatus["items"]:
            for code, cell in it["cells"].items():
                st = cell.get("state")
                if st == "OVERDUE":
                    _add("원내교육", course_title.get(code, code), it["name"],
                         date.fromisoformat(str(cell["valid_until"])[:10]), "이수만료")
                elif st == "DUE_SOON":
                    _add("원내교육", course_title.get(code, code), it["name"],
                         date.fromisoformat(str(cell["valid_until"])[:10]), "이수")
                elif st == "NONE":
                    _add("원내교육", course_title.get(code, code), it["name"], as_of, "미이수")

        # ③ 4대보험 신고 기한(hr.InsuranceReportEvent) — 미처리(PENDING)만
        for ev in (await session.execute(
                select(InsuranceReportEvent).where(InsuranceReportEvent.status == "PENDING",
                                                   InsuranceReportEvent.due_on <= cutoff)
                .order_by(InsuranceReportEvent.due_on))).scalars():
            _add("4대보험 신고", ev.event, emp_name.get(ev.employee_id), ev.due_on, "미신고")

        # ④ 방사선 장비 정기검사 만기(regulatory.RadiationEquipment)
        for eq in (await session.execute(
                select(RadiationEquipment).where(RadiationEquipment.next_inspection_on <= cutoff)
                .order_by(RadiationEquipment.next_inspection_on))).scalars():
            _add("방사선 장비검사", eq.name, eq.equipment_no, eq.next_inspection_on, "검사대기")

        # ⑤ 재고 유효기간 임박(scm.Lot) — 잔량 있는 로트만
        for lot, item_name in (await session.execute(
                select(Lot, Item.name).join(Item, Item.id == Lot.item_id)
                .where(Lot.remaining_qty > 0, Lot.expiry_on <= cutoff)
                .order_by(Lot.expiry_on))).all():
            _add("재고 유효기간", f"{item_name} (로트 {lot.lot_no})",
                 f"잔량 {float(lot.remaining_qty):g}", lot.expiry_on, "재고보유")

        # ⑥ 위수탁·구매 계약 만기(scm.OutsourcingContract) — 유효(ACTIVE/EXPIRING) 계약만
        for oc in (await session.execute(
                select(OutsourcingContract).where(
                    OutsourcingContract.status.in_(_LIVE_CONTRACT),
                    OutsourcingContract.end_on <= cutoff)
                .order_by(OutsourcingContract.end_on))).scalars():
            _add("위수탁·구매 계약", oc.title, oc.vendor_name, oc.end_on,
                 "자동갱신" if oc.auto_renew else "갱신검토")

        # ⑦ 의료진 계약 만기(hr.MedicalStaffContract) — 유효 계약·종료일 설정분
        for mc in (await session.execute(
                select(MedicalStaffContract).where(
                    MedicalStaffContract.status.in_(_LIVE_CONTRACT),
                    MedicalStaffContract.valid_to.isnot(None),
                    MedicalStaffContract.valid_to <= cutoff)
                .order_by(MedicalStaffContract.valid_to))).scalars():
            _add("의료진 계약", emp_name.get(mc.employee_id) or f"직원#{mc.employee_id}",
                 None, mc.valid_to, "갱신검토")

        # ⑧ 계약직 근로계약 만기(hr.Employee) — 재직 중·계약종료일 설정분
        for emp in (await session.execute(
                select(Employee).where(
                    Employee.contract_end.isnot(None), Employee.resigned_on.is_(None),
                    Employee.contract_end <= cutoff)
                .order_by(Employee.contract_end))).scalars():
            _add("근로계약(계약직)", emp.name, emp.employment_type, emp.contract_end, "갱신검토")

        # ⑧-2 법정 파라미터 연도 갱신(거버넌스) — 내년 4대보험 요율·소득세 파라미터 미등재 시
        # 연말 만기(미등재 상태로 해 넘기면 급여·세액 계산이 fail-safe 예외로 정지 — 사전 경보).
        from erp.hr.statutory import refresh_db_params, registered_years
        await refresh_db_params(session)     # 관리화면 등록분 포함(코드 상수만 보면 등록해도 경보 지속)
        reg = registered_years()
        ny = as_of.year + 1
        yend = date(as_of.year, 12, 31)
        if yend <= cutoff:
            if ny not in reg["insurance"]:
                _add("법정 파라미터", f"4대보험 요율 {ny}년 등록(/admin/payroll-settings)", None,
                     yend, "미등록")
            if ny not in reg["tax"]:
                _add("법정 파라미터", f"소득세 파라미터 {ny}년 등록(개정 없음=전년 승계)", None,
                     yend, "미등록")
        # ⑧-3 이듬해 공휴일(음력 명절·대체공휴일) 미등록 — 아무것도 멈추지 않지만 휴일근로 가산 과소·사전심사
        # 공휴가산 오탐이 조용히 생긴다(2026-10-10 연말 대비). 출구=관리자 > 기관 설정 '공휴일 캘린더'.
        if yend <= cutoff:
            from erp.common.models import Holiday
            n_next = await session.scalar(select(func.count()).select_from(Holiday).where(
                Holiday.on_date >= date(ny, 1, 1), Holiday.on_date <= date(ny, 12, 31)))
            if not n_next:
                _add("법정 파라미터", f"{ny}년 공휴일(설·추석·대체공휴일) 등록(/admin/org '공휴일 캘린더')", None,
                     yend, "미등록")
        # 국민연금 기준소득월액 상·하한 — 매년 7월 고시 반영(연례 점검·M-12 운영 절차)
        np_due = (date(as_of.year, 7, 1) if as_of <= date(as_of.year, 7, 1)
                  else date(as_of.year + 1, 7, 1))
        if np_due <= cutoff:
            # 연도 요율에 '7월분부터' 상·하한이 들어 있으면 이미 반영된 것 — 그 해 것이 없을 때만 경보
            # (종전 문구는 코드 상수 RATES 수정을 가리켰다 — 관리화면 '7월분부터' 칸이 출구·2026-10-10)
            from erp.errors import ValidationError as _VE
            from erp.hr.statutory import insurance_rates
            try:
                has_h2 = bool(insurance_rates(np_due.year).get("pension_max_h2"))
            except _VE:          # 그 해 요율 미등록 — 위 '미등록' 경보가 따로 뜬다(다른 오류는 숨기지 않는다)
                has_h2 = False
            if not has_h2:
                _add("법정 파라미터", f"국민연금 상·하한 {np_due.year}년 7월분 반영"
                     "(/admin/payroll-settings '7월분부터' 칸)", None, np_due, "연례 점검")

        # ⑨ 의료폐기물 법정 보관기한(scm.MedicalWasteLog) — 미인계(STORED)·보관기간 도래분.
        # due = 보관시작 + 법정 보관기간(종류별). 초과 시 폐기물관리법 위반이라 조기 경고.
        for w in (await session.execute(
                select(MedicalWasteLog).where(MedicalWasteLog.status == "STORED")
                .order_by(MedicalWasteLog.stored_on))).scalars():
            limit = _WASTE_STORAGE_DAYS.get(w.waste_type, _WASTE_STORAGE_DEFAULT)
            due = w.stored_on + timedelta(days=limit)
            if due <= cutoff:
                _add("의료폐기물 보관", f"{w.waste_type} {float(w.weight_kg):g}kg",
                     f"보관 {w.stored_on}", due, "미인계")

        # ⑩ SW·서비스 구독 만기(itam.SoftwareService·해지분 제외) + 자동갱신 해지 데드라인
        for s in (await session.execute(
                select(SoftwareService).where(SoftwareService.expires_on.isnot(None),
                                              SoftwareService.status == "ACTIVE",
                                              SoftwareService.expires_on <= cutoff)
                .order_by(SoftwareService.expires_on))).scalars():
            _add("SW·서비스 구독", f"{s.name}{f' ({s.vendor})' if s.vendor else ''}", s.service_type,
                 s.expires_on, "자동갱신" if s.auto_renew else "갱신검토")
            if s.auto_renew and s.cancel_notice_days:
                cancel_by = s.expires_on - timedelta(days=s.cancel_notice_days)
                if cancel_by <= cutoff:
                    _add("구독 해지기한", f"{s.name} 해지 통보", s.vendor, cancel_by, "해지검토")

        # ⑪ IT 자산 보증 만기(itam.ITAsset) — 유효 상태만
        for a in (await session.execute(
                select(ITAsset).where(ITAsset.warranty_until.isnot(None),
                                      ITAsset.status.in_(_ITAM_LIVE),
                                      ITAsset.warranty_until <= cutoff)
                .order_by(ITAsset.warranty_until))).scalars():
            _add("IT 자산 보증", f"{a.name} ({a.asset_tag})", a.category, a.warranty_until, "보증만료")

        # ⑫ IT 벤더 계약 만기(itam.VendorContract)
        for vc in (await session.execute(
                select(VendorContract).where(VendorContract.end_on.isnot(None),
                                             VendorContract.end_on <= cutoff)
                .order_by(VendorContract.end_on))).scalars():
            _add("IT 벤더 계약", vc.title, vc.vendor, vc.end_on, "갱신검토")

        # ⑬ 의료 IT 기기 교정/정도관리 만기(itam.ITAsset.calibration_due) — 유효 상태만
        for a in (await session.execute(
                select(ITAsset).where(ITAsset.calibration_due.isnot(None),
                                      ITAsset.status.in_(_ITAM_LIVE),
                                      ITAsset.calibration_due <= cutoff)
                .order_by(ITAsset.calibration_due))).scalars():
            _add("의료기기 교정", f"{a.name} ({a.asset_tag})", a.udi or a.mfds_no,
                 a.calibration_due, "교정대기")

        # ⑭ IT 자산 정기점검 예정(itam.ITAsset.next_inspection_on) — 유효 상태만
        for a in (await session.execute(
                select(ITAsset).where(ITAsset.next_inspection_on.isnot(None),
                                      ITAsset.status.in_(_ITAM_LIVE),
                                      ITAsset.next_inspection_on <= cutoff)
                .order_by(ITAsset.next_inspection_on))).scalars():
            _add("IT 정기점검", f"{a.name} ({a.asset_tag})", a.category,
                 a.next_inspection_on, "점검예정")

        # ⑮ IT 자산 회수 예정(itam.ITAsset.return_due_on) — 대여/임시 반납 예정
        for a in (await session.execute(
                select(ITAsset).where(ITAsset.return_due_on.isnot(None),
                                      ITAsset.status.in_(_ITAM_LIVE),
                                      ITAsset.return_due_on <= cutoff)
                .order_by(ITAsset.return_due_on))).scalars():
            _add("IT 자산 회수", f"{a.name} ({a.asset_tag})", a.location,
                 a.return_due_on, "회수예정")

        # ⑯ IT 자산 정비 예정(itam.ITAsset.next_maintenance_on) — 유효 상태만
        for a in (await session.execute(
                select(ITAsset).where(ITAsset.next_maintenance_on.isnot(None),
                                      ITAsset.status.in_(_ITAM_LIVE),
                                      ITAsset.next_maintenance_on <= cutoff)
                .order_by(ITAsset.next_maintenance_on))).scalars():
            _add("IT 정비 예정", f"{a.name} ({a.asset_tag})", a.category,
                 a.next_maintenance_on, "정비예정")

        # 실행 우선 정렬 — 장기경과는 하단으로(임박·최근경과가 위), 그 외 due_on 오름차순.
        items.sort(key=lambda x: (x["band"] == "long_overdue", x["due_on"]))
        by_band: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for it in items:
            by_band[it["band"]] = by_band.get(it["band"], 0) + 1
            # 카테고리 요약은 실행 대상(장기경과 제외)만 — 노이즈 억제
            if it["band"] != "long_overdue":
                by_category[it["category"]] = by_category.get(it["category"], 0) + 1

        return {
            "as_of": str(as_of), "horizon_days": horizon_days,
            "summary": {
                "total": len(items),
                "actionable": len(items) - by_band.get("long_overdue", 0),
                "overdue": by_band.get("overdue", 0), "d7": by_band.get("d7", 0),
                "d30": by_band.get("d30", 0), "d90": by_band.get("d90", 0),
                "long_overdue": by_band.get("long_overdue", 0),
                "by_category": by_category},
            "items": items,
            "alerts": await self._alerts(session, as_of=as_of, emp_name=emp_name),
        }

    async def _alerts(self, session: AsyncSession, *, as_of: date,
                      emp_name: dict) -> dict:
        """비기한형 경보 — 방사선 피폭 연한도 근접/초과·환자안전 미검토(REPORTED/REVIEWING)."""
        year = str(as_of.year)
        rad = (await session.execute(
            select(RadiationDose.employee_id,
                   func.coalesce(func.sum(RadiationDose.dose_msv), 0))
            .where(RadiationDose.period.like(f"{year}%"))
            .group_by(RadiationDose.employee_id))).all()
        over, near = [], []
        for emp_id, ytd in rad:
            ytd = float(ytd)
            row = {"employee": emp_name.get(emp_id), "ytd_msv": round(ytd, 3),
                   "limit_msv": _RAD_ANNUAL_LIMIT_MSV}
            if ytd > _RAD_ANNUAL_LIMIT_MSV:
                over.append(row)
            elif ytd >= _RAD_ANNUAL_LIMIT_MSV * _RAD_NEAR_RATIO:
                near.append(row)

        # 5년 누적 100mSv 한도(법정 병행 한도 — 계산검토 M-7·종전 연 50만 점검) — 최근 5개년 합
        year5 = [str(y) for y in range(as_of.year - 4, as_of.year + 1)]
        rad5 = (await session.execute(
            select(RadiationDose.employee_id,
                   func.coalesce(func.sum(RadiationDose.dose_msv), 0))
            .where(func.substr(RadiationDose.period, 1, 4).in_(year5))
            .group_by(RadiationDose.employee_id))).all()
        over5 = [{"employee": emp_name.get(e), "cum5_msv": round(float(v), 3),
                  "limit_msv": _RAD_5Y_LIMIT_MSV}
                 for e, v in rad5 if float(v) > _RAD_5Y_LIMIT_MSV]

        safety_unreviewed = int((await session.scalar(
            select(func.count()).select_from(PatientSafetyIncident)
            .where(PatientSafetyIncident.status.in_(("REPORTED", "REVIEWING")))) or 0))

        # 의약품 회수 미조치(식약처 회수명령) — OPEN = 영향 로트 격리/폐기 미완료
        drug_recall_open = int((await session.scalar(
            select(func.count()).select_from(DrugRecall)
            .where(DrugRecall.status == "OPEN")) or 0))

        return {"radiation_over_limit": over, "radiation_near_limit": near,
                "radiation_over_5y_limit": over5,
                "safety_unreviewed": safety_unreviewed, "drug_recall_open": drug_recall_open}
