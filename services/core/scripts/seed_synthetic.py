"""합성 테스트 데이터 시드 — Plan §4.4 테스트 데이터 정책 (M1 산출물).

원칙: 실 환자·직원 데이터 사용 금지 (개인정보보호법). 전부 합성:
  ① 환자 500 — TEST- 접두 차트번호, 임의 생년월일
  ② 직원 150 — 가상 인사 (간호 3교대 직군 분포, 전공의 포함)
  ③ 수가 마스터 — 코드 50종 × 2버전 (temporal 경계 테스트용)
  ④ 듀티 1개월 — 실제 근무 패턴 모사 (D/E/N 로테이션)
  ⑤ 품목 30종 (마약류 3종 포함) + 초기 재고
  ⑥ 관리자/부서장/직원 계정

실행: services/core에서  .venv/bin/python scripts/seed_synthetic.py
대상: DATABASE_URL (기본 dev compose PG) — 운영 DB에 실행 금지!
"""
import asyncio
import os
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

random.seed(20260607)  # 재현 가능한 시드

SURNAMES = "김이박최정강조윤장임한오서신권황안송류전홍"
GIVEN = ["민준", "서연", "도윤", "지우", "하은", "시우", "수아", "지호", "유나", "준서",
         "예은", "건우", "다은", "현우", "소율", "지안", "우진", "서윤", "은우", "채원"]


def _name(i: int) -> str:
    return SURNAMES[i % len(SURNAMES)] + GIVEN[(i * 7) % len(GIVEN)]


def _birth(i: int) -> date:
    return date(1950 + (i * 13) % 60, 1 + (i * 5) % 12, 1 + (i * 11) % 28)


async def main() -> None:
    from sqlalchemy import select, func
    from erp.db import session_factory
    from erp.common.models import Department, User
    from erp.common.security import hash_password
    from erp.hr.models import DutyAssignment, DutySchedule, Employee, License
    from erp.patient.service import PatientService
    from erp.regulatory.sync import FeeSyncService, FeeUpdate
    from erp.scm.models import Item, Lot

    factory = session_factory()
    async with factory() as session:
        async with session.begin():
            existing = (await session.execute(
                select(func.count()).select_from(User))).scalar_one()
            if existing:
                print(f"이미 데이터 존재 (users={existing}) — 시드 생략 (멱등)")
                return

            # ── 부서 ───────────────────────────────────────
            depts = {}
            for name in ["원무과", "간호부", "약제팀", "의료보험팀", "내과", "정형외과", "행정팀"]:
                d = Department(name=name)
                session.add(d)
                await session.flush()
                depts[name] = d.id

            # ── 계정 ───────────────────────────────────────
            pw = hash_password("Erp!Demo2026")
            session.add_all([
                User(email="director@demo-hospital.example", password_hash=pw, role="DIRECTOR"),
                User(email="admin@demo-hospital.example", password_hash=pw, role="ADMIN",
                     department_id=depts["행정팀"]),
                User(email="nurse.manager@demo-hospital.example", password_hash=pw, role="MANAGER",
                     department_id=depts["간호부"]),
                User(email="staff@demo-hospital.example", password_hash=pw, role="STAFF",
                     department_id=depts["간호부"]),
            ])

            # ── 직원 150 (간호 60·의사 20·전공의 10·기사 20·행정 40) ──
            groups = [("NURSE", 60, "간호부"), ("DOCTOR", 20, "내과"),
                      ("RESIDENT", 10, "내과"), ("TECH", 20, "정형외과"),
                      ("STAFF", 40, "행정팀")]
            employees: list[Employee] = []
            seq = 0
            for group, count, dept in groups:
                for _ in range(count):
                    seq += 1
                    emp = Employee(
                        employee_no=f"E{seq:04d}", name=_name(seq),
                        hired_on=date(2018 + seq % 8, 1 + seq % 12, 1),
                        department_id=depts[dept], job_group=group,
                        position="팀원" if seq % 7 else "팀장",
                        birth_date=_birth(seq))
                    session.add(emp)
                    employees.append(emp)
            await session.flush()
            # 간호사 면허 (일부 만료 임박 — 알림 테스트)
            for i, emp in enumerate(e for e in employees if e.job_group == "NURSE"):
                session.add(License(
                    employee_id=emp.id, license_type="간호사", license_no=f"RN-{i:05d}",
                    issued_on=date(2018, 1, 1),
                    expires_on=date.today() + timedelta(days=[400, 30, 7, 200][i % 4])))

            # ── 환자 500 (TEST- 차트) ──────────────────────
            patients = PatientService()
            for i in range(500):
                await patients.create(
                    session, chart_no=f"TEST-{i:05d}", name=_name(i + 1000),
                    birth_date=_birth(i),
                    phone=f"010-{1000 + i % 9000:04d}-{i % 10000:04d}" if i % 3 else None)

            # ── 수가 마스터 50코드 × 2버전 (temporal 경계) ──
            sync = FeeSyncService()
            v1 = [FeeUpdate(code=f"AA{i:03d}", code_type="TARIFF", name=f"진료행위{i}",
                            unit_price=10_000 + i * 500, valid_from=date(2026, 1, 1))
                  for i in range(50)]
            await sync.apply(session, source_version="seed-v1", updates=v1)
            v2 = [FeeUpdate(code=f"AA{i:03d}", code_type="TARIFF", name=f"진료행위{i}",
                            unit_price=10_300 + i * 500, valid_from=date(2026, 7, 1))
                  for i in range(50)]
            await sync.apply(session, source_version="seed-v2", updates=v2, force=True)

            # ── 품목 30 (마약류 3) + 재고 ──────────────────
            for i in range(30):
                item = Item(code=f"MED-{i:03d}", name=f"의약품{i}", category="DRUG",
                            is_narcotic=(i < 3), safety_stock_qty=20)
                session.add(item)
                await session.flush()
                session.add(Lot(item_id=item.id, lot_no=f"L-{i:03d}",
                                expiry_on=date.today() + timedelta(days=60 + i * 30),
                                received_on=date.today() - timedelta(days=30),
                                received_qty=100, remaining_qty=100))

            # ── 듀티 1개월 (간호부 — D/E/N 로테이션 모사) ──
            nurses = [e for e in employees if e.job_group == "NURSE"][:30]
            month_start = date.today().replace(day=1)
            schedule = DutySchedule(department_id=depts["간호부"],
                                    yyyymm=month_start.strftime("%Y-%m"))
            session.add(schedule)
            await session.flush()
            pattern = ["D", "D", "E", "E", "N", "N", "OFF", "OFF"]
            day = month_start
            while day.month == month_start.month:
                for idx, nurse in enumerate(nurses):
                    shift = pattern[(day.day + idx * 2) % len(pattern)]
                    session.add(DutyAssignment(schedule_id=schedule.id, employee_id=nurse.id,
                                               duty_on=day, shift=shift))
                day += timedelta(days=1)

            print("시드 완료: 부서 7 · 계정 4 · 직원 150 · 환자 500(TEST-) · "
                  "수가 50×2버전 · 품목 30(마약류 3) · 듀티 1개월(간호 30명)")
            print("로그인: admin@demo-hospital.example / Erp!Demo2026 (director·nurse.manager·staff 동일)")


if __name__ == "__main__":
    asyncio.run(main())
