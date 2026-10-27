"""ITAM(IT 자산관리) 시드 (테스트용) — 구체화된 하드웨어·SW서비스·비용·유지보수·계약.

개발/시연용 합성 데이터(TEST- 마커·제거 가능). **upsert**: 이미 있으면 구체 필드까지 갱신.
하드웨어=장비 디테일(spec)·도입일·정기점검/정비/회수 예정, SW=등록일/개시일/유료 첫 결제일/결제방식.

사용: python scripts/seed_itam_demo.py
대상: DATABASE_URL — 테스트/파일럿 DB.
"""
import asyncio
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import select  # noqa: E402

from erp.db import session_factory  # noqa: E402
from erp.hr.models import Employee  # noqa: E402
from erp.itam.cost_service import CostService  # noqa: E402
from erp.itam.models import ITAsset, SoftwareService, VendorContract  # noqa: E402
from erp.itam.service import ITAMService  # noqa: E402
from erp.itam.software_service import SoftwareServiceService  # noqa: E402

_SYSTEM = 1
_TODAY = date(2026, 7, 9)


def _d(days: int) -> date:
    return _TODAY + timedelta(days=days)


# 구체화된 하드웨어 — (cat, name, mfr, model, phi, status, spec, 도입_경과일, 점검주기일, 회수예정일)
_HW = [
    ("LAPTOP", "진료용 노트북(내과)", "삼성", "갤럭시북4 프로", True, "ASSIGNED",
     {"cpu": "Core Ultra 7 155H", "ram": "16GB", "disk": "512GB NVMe", "os": "Win11 Pro"}, -420, None, None),
    ("LAPTOP", "간호 스테이션 노트북", "LG", "그램16 2024", True, "ASSIGNED",
     {"cpu": "Core i7-1360P", "ram": "16GB", "disk": "512GB NVMe", "os": "Win11 Pro"}, -300, None, None),
    ("LAPTOP", "행정 노트북", "삼성", "갤럭시북3", False, "ASSIGNED",
     {"cpu": "Core i5-1340P", "ram": "8GB", "disk": "256GB SSD", "os": "Win11 Pro"}, -560, None, None),
    ("DESKTOP", "원무과 접수 데스크톱", "HP", "ProDesk 400 G9", True, "ASSIGNED",
     {"cpu": "Core i5-12500", "ram": "16GB", "disk": "512GB SSD", "os": "Win11 Pro"}, -700, None, None),
    ("DESKTOP", "약제부 조제 데스크톱", "델", "OptiPlex 7010", True, "IN_STOCK",
     {"cpu": "Core i5-13500", "ram": "16GB", "disk": "512GB SSD", "os": "Win11 Pro"}, -120, None, None),
    ("DESKTOP", "회계팀 데스크톱", "HP", "EliteDesk 800 G9", False, "ASSIGNED",
     {"cpu": "Core i7-12700", "ram": "32GB", "disk": "1TB SSD", "os": "Win11 Pro"}, -380, None, None),
    ("SERVER", "EMR DB 서버", "델", "PowerEdge R750", True, "ASSIGNED",
     {"cpu": "Xeon Gold 6338 ×2", "ram": "256GB", "disk": "8TB SAS RAID10", "os": "RHEL 9"}, -900, 90, None),
    ("SERVER", "PACS 스토리지", "HPE", "ProLiant DL380", True, "ASSIGNED",
     {"cpu": "Xeon Silver 4314 ×2", "ram": "128GB", "disk": "48TB NL-SAS RAID6", "os": "Win Server 2022"}, -1100, 90, None),
    ("SERVER", "백업 서버", "델", "PowerEdge R650", False, "REPAIR",
     {"cpu": "Xeon Silver 4310", "ram": "64GB", "disk": "24TB RAID6", "os": "Ubuntu 22.04"}, -740, 180, None),
    ("NETWORK", "코어 스위치", "시스코", "Catalyst 9300-48P", False, "ASSIGNED",
     {"ports": "48×1G PoE+", "uplink": "4×10G SFP+", "firmware": "IOS-XE 17.9"}, -650, 180, None),
    ("NETWORK", "병동 무선 AP", "아루바", "AP-515", False, "ASSIGNED",
     {"wifi": "Wi-Fi 6 (802.11ax)", "poe": "802.3at", "firmware": "ArubaOS 8.11"}, -400, None, None),
    ("NETWORK", "인터넷 방화벽", "포티넷", "FortiGate 100F", True, "ASSIGNED",
     {"throughput": "10Gbps", "sessions": "1.5M", "firmware": "FortiOS 7.4"}, -500, 180, None),
    ("PRINTER", "원무 복합기", "신도리코", "D410", False, "ASSIGNED",
     {"type": "흑백 레이저 복합", "ppm": "40ppm", "iface": "유선LAN"}, -300, None, None),
    ("PRINTER", "약국 라벨 프린터", "지브라", "ZD421", False, "IN_STOCK",
     {"type": "감열 라벨", "dpi": "203dpi", "iface": "USB/LAN"}, -90, None, None),
    ("MOBILE", "회진용 태블릿", "애플", "iPad 10세대", True, "ASSIGNED",
     {"os": "iPadOS 17", "storage": "64GB", "cellular": "Wi-Fi"}, -260, None, None),
    ("MOBILE", "물류 스캐너폰(대여)", "삼성", "XCover6 Pro", False, "LOANER",
     {"os": "Android 13", "scanner": "내장 바코드", "storage": "128GB"}, -180, None, 20),
    ("MEDICAL_IT", "초음파 워크스테이션", "GE", "LOGIQ E10", True, "ASSIGNED",
     {"cpu": "Xeon W", "ram": "32GB", "modality": "US", "monitor": "23\" LCD"}, -820, None, None),
    ("MEDICAL_IT", "내시경 영상 PC", "올림푸스", "EVIS X1", True, "ASSIGNED",
     {"cpu": "Core i7", "ram": "32GB", "modality": "Endo", "capture": "4K"}, -600, None, None),
    ("MEDICAL_IT", "혈액분석기 연동 PC", "지멘스", "Atellica IM", True, "IN_STOCK",
     {"cpu": "Core i5", "ram": "16GB", "modality": "Lab", "iface": "ASTM/HL7"}, -300, None, None),
    ("PERIPHERAL", "수납 카드 리더기", "블루버드", "BR-500", False, "ASSIGNED",
     {"iface": "USB", "emv": "IC/MSR/NFC"}, -200, None, None),
    ("PERIPHERAL", "전자서명 패드", "와콤", "STU-540", False, "IN_STOCK",
     {"iface": "USB", "display": "컬러 LCD", "res": "800×480"}, -150, None, None),
    ("DESKTOP", "구형 접수 PC(폐기예정)", "삼성", "DB400T7A", False, "RETIRED",
     {"cpu": "Core i3-8100", "ram": "8GB", "disk": "256GB SSD", "os": "Win10"}, -2200, None, None),
    ("LAPTOP", "분실 신고 노트북", "LG", "그램14 2023", False, "LOST",
     {"cpu": "Core i5-1340P", "ram": "16GB", "disk": "512GB NVMe", "os": "Win11 Pro"}, -500, None, None),
    ("PRINTER", "노후 복합기", "캐논", "iR2625", False, "IN_STOCK",
     {"type": "흑백 레이저 복합", "ppm": "25ppm", "iface": "유선LAN"}, -1800, None, None),
]

# 구체화된 SW·서비스 — (name, vendor, type, billing, seats, cur, recurring, 만기경과일, auto, notice,
#                       등록경과일, 개시경과일, 첫결제경과일, 결제방식)
_SW = [
    ("TEST-Microsoft 365 E3", "Microsoft", "SAAS", "SEAT_RECURRING", 120, "USD", 15, 200, True, 30,
     -730, -720, -720, "POSTPAID"),
    ("TEST-Windows Server 2022 라이선스", "Microsoft", "PERPETUAL_SW", "ONE_TIME", 20, "KRW", None, None, False, None,
     -900, -890, -890, "PREPAID"),
    ("TEST-Adobe Acrobat Pro", "Adobe", "SAAS", "SEAT_RECURRING", 15, "USD", 23, 120, True, 30,
     -400, -395, -395, "PREPAID"),
    ("TEST-Slack Business+", "Salesforce", "SAAS", "SEAT_RECURRING", 80, "USD", 8, 45, True, 30,
     -600, -590, -560, "POSTPAID"),
    ("TEST-ChatGPT Team", "OpenAI", "AI_SUBSCRIPTION", "SEAT_RECURRING", 25, "USD", 30, 90, True, 14,
     -200, -195, -180, "PREPAID"),
    ("TEST-GitHub Copilot Business", "GitHub", "AI_SUBSCRIPTION", "SEAT_RECURRING", 10, "USD", 19, 150, True, 14,
     -160, -158, -158, "POSTPAID"),
    ("TEST-AWS (클라우드 인프라)", "Amazon", "CLOUD", "USAGE_METERED", None, "USD", None, None, False, None,
     -1000, -995, -965, "POSTPAID"),
    ("TEST-한글과컴퓨터 오피스", "한컴", "PERPETUAL_SW", "ONE_TIME", 200, "KRW", None, None, False, None,
     -1200, -1195, -1195, "PREPAID"),
    ("TEST-V3 엔드포인트 백신", "안랩", "SAAS", "SEAT_RECURRING", 300, "KRW", 500000, 220, True, 30,
     -800, -790, -760, "PREPAID"),
    ("TEST-Claude Team (무료체험)", "Anthropic", "AI_SUBSCRIPTION", "SEAT_RECURRING", 5, "USD", 30, 25, False, 7,
     -20, -18, 12, "FREE_TRIAL"),
]


async def main() -> None:
    itam, sw, cost = ITAMService(), SoftwareServiceService(), CostService()
    async with session_factory()() as session:
        emps = (await session.scalars(select(Employee).where(
            Employee.resigned_on.is_(None)).order_by(Employee.id).limit(30))).all()
        emp_ids = [e.id for e in emps]
        n_new = n_upd = 0

        # ── 벤더 계약(upsert) ──
        contracts = {}
        for vendor, title, kind, days in [
                ("델", "TEST-서버 유지보수 AMC", "AMC", 300), ("Microsoft", "TEST-M365 구독계약", "SUBSCRIPTION", 200),
                ("시스코", "TEST-네트워크 장비 구매", "PURCHASE", None), ("GE", "TEST-의료장비 유지보수", "AMC", 150)]:
            exist = await session.scalar(select(VendorContract).where(VendorContract.title == title))
            if exist:
                contracts[title] = exist.id
                continue
            c = await itam.create_contract(session, actor=_SYSTEM, vendor=vendor, title=title,
                                           kind=kind, amount=5_000_000, end_on=_d(days) if days else None)
            contracts[title] = c.id

        # ── 하드웨어(upsert·구체 필드) ──
        for i, (cat, name, mfr, model, phi, status, spec, acq, insp, ret) in enumerate(_HW):
            tag = f"TEST-IT-{i + 1:04d}"
            fields = dict(
                name=name, category=cat, manufacturer=mfr, model=model, status=status,
                handles_phi=phi, spec=spec, acquired_on=_d(acq),
                warranty_until=_d(acq + 1095),                       # 보증 3년
                location=["3층 원무과", "5층 병동", "2층 검사실", "본관 전산실", "약제부"][i % 5],
                ip_addr=f"10.10.{i}.{20 + i}" if cat in ("SERVER", "NETWORK") else None,
                disk_encrypted=phi, av_installed=True,
                next_inspection_on=_d(insp // 2) if insp else None,  # 다음 점검 예정(주기의 절반 후)
                next_maintenance_on=_d(40) if cat in ("SERVER", "MEDICAL_IT") else None,
                return_due_on=_d(ret) if ret else None,
                calibration_due=_d(25) if cat == "MEDICAL_IT" else None,
                mfds_no=f"수허20{i:03d}" if cat == "MEDICAL_IT" else None)
            existing = await session.scalar(select(ITAsset).where(ITAsset.asset_tag == tag))
            if existing:
                fields.pop("status", None)   # 재실행 시 운영 상태 보존(전이 가드와 충돌 방지)
                await itam.update_asset(session, actor=_SYSTEM, asset_id=existing.id, **fields)
                n_upd += 1
                aid = existing.id
                already = existing.assigned_employee_id
            else:
                cfields = dict(fields)
                if cfields.get("status") == "ASSIGNED":
                    cfields["status"] = "IN_STOCK"    # 배정은 assign()으로(전이 가드 S-1)
                a = await itam.create_asset(session, actor=_SYSTEM, asset_tag=tag,
                                            serial_no=f"TEST-SN-{i + 1:04d}", **cfields)
                n_new += 1
                aid, already = a.id, None
            if status == "ASSIGNED" and not already and emp_ids:
                await itam.assign(session, actor=_SYSTEM, asset_id=aid, employee_id=emp_ids[i % len(emp_ids)])
            print(f"  {tag} {name}", end="\r")

        # ── SW·서비스(upsert·날짜/결제방식) ──
        n_sw = 0
        for (name, vendor, stype, bmodel, seats, cur, rec, expd, auto, notice,
             regd, startd, billd, terms) in _SW:
            dates = dict(registered_on=_d(regd), started_on=_d(startd), first_billing_on=_d(billd),
                         payment_terms=terms, expires_on=_d(expd) if expd else None,
                         auto_renew=auto, cancel_notice_days=notice, currency=cur,
                         recurring_cost=rec, vendor=vendor, seats=seats)
            exist = await session.scalar(select(SoftwareService).where(SoftwareService.name == name))
            if exist:
                await sw.update(session, actor=_SYSTEM, service_id=exist.id, **dates)
                n_sw += 1
                continue
            s = await sw.create(session, actor=_SYSTEM, name=name, service_type=stype,
                                billing_model=bmodel, **dates,
                                license_key=f"TEST-KEY-{name[:4].upper()}-9999" if stype == "PERPETUAL_SW" else None)
            n_sw += 1
            if seats and emp_ids:
                used = min(seats + (2 if name.startswith("V3") else -3), len(emp_ids))
                for k in range(max(0, used)):
                    await sw.allocate(session, actor=_SYSTEM, service_id=s.id, employee_id=emp_ids[k % len(emp_ids)])
            if bmodel in ("SEAT_RECURRING", "USAGE_METERED"):
                base = (rec or 100) * (seats or 1) if cur != "KRW" else (rec or 500000)
                fx = 1350 if cur == "USD" else None
                mults = [1.0, 1.0, 2.2] if stype == "CLOUD" else [1.0, 1.0, 1.05]
                for mo, mult in zip(["2026-05", "2026-06", "2026-07"], mults):
                    await cost.record_cost(session, actor=_SYSTEM, service_id=s.id, period=mo,
                                           amount=base * mult, currency=cur, fx_rate=fx,
                                           usage_note="vCPU·스토리지·전송" if stype == "CLOUD" else None)

        await session.commit()
    print(f"\n완료: 하드웨어 신규 {n_new}·갱신 {n_upd} · SW/서비스 {n_sw} · 계약 {len(contracts)}")


if __name__ == "__main__":
    asyncio.run(main())
