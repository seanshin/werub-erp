"""시드 정합 조정 — HIS 진료 라인의 환자를 ERP 환자 마스터에 생성(개발 환경).

배경(2026-07-29 발생주의 시뮬레이션에서 발견):
  `patient.billing_lines`의 환자 59명은 HIS 실차트(`HR-…`·`P2026-…`)인데 ERP `patient.patients`
  500명은 전부 `TEST-` 데모 시드라 **교집합이 0명**이었다. `ReceivableService._collected`는
  `Payment → Reception → Patient.chart_no`로 조인해 수납을 찾으므로, 이 상태에서는
  **수납이 있어도 못 찾고 본인부담 전액이 미수로 계산**된다(발생주의 전환 시 1131 과대계상).

이 스크립트가 하는 일(멱등):
  진료 라인에 있는 chart_no 중 마스터에 없는 것을 생성한다. 이름·생년월일은 HIS가 주지 않으므로
  ⚠ **합성값**을 넣고 그 사실을 이름에 남긴다(`(연동)` 접미) — 실 운영에서는 HIS 환자 동기화가
  이 자리를 대신해야 하며, 그때까지 개발 화면이 조인 실패로 비어 보이는 것을 막는 용도다.
  라인의 `patient_sex`·`patient_age`는 있으면 생년월일 추정에 쓴다(연도만·1월 1일).

실행:
  docker cp .../fix_seed_patient_master.py infra-core-1:/tmp/
  docker exec -w /app -e PYTHONPATH=/app/src infra-core-1 python /tmp/fix_seed_patient_master.py [--apply]
"""
import asyncio
import sys
from datetime import date

from sqlalchemy import select

from erp.common.kst import kst_today
from erp.db import session_factory
from erp.patient.models import BillingLine, Patient


async def main() -> None:
    apply = "--apply" in sys.argv
    async with session_factory()() as s:
        rows = (await s.execute(select(
            BillingLine.chart_no, BillingLine.patient_age, BillingLine.patient_sex,
            BillingLine.treated_on).where(BillingLine.cancelled.is_(False)))).all()
        # chart별 대표값 — 가장 최근 진료 라인 기준
        by_chart: dict[str, tuple] = {}
        for chart_no, age, sex, treated_on in rows:
            if not chart_no:
                continue
            cur = by_chart.get(chart_no)
            if cur is None or (treated_on and cur[2] and treated_on > cur[2]):
                by_chart[chart_no] = (age, sex, treated_on)

        existing = set((await s.scalars(select(Patient.chart_no).where(
            Patient.chart_no.in_(list(by_chart))))).all())
        missing = {c: v for c, v in by_chart.items() if c not in existing}

        print(f"진료 라인 환자 {len(by_chart)}명 · 마스터 보유 {len(existing)}명 · "
              f"생성 대상 {len(missing)}명")
        if not missing:
            print("  정합 — 생성할 것 없음")
            return

        today = kst_today()
        for chart_no, (age, sex, treated_on) in sorted(missing.items()):
            # 생년월일: 나이가 있으면 연도만 역산(1월 1일), 없으면 1980-01-01.
            birth = date(today.year - int(age), 1, 1) if age else date(1980, 1, 1)
            label = f"{chart_no} (연동)"          # 합성 마스터임을 이름에 남긴다
            if apply:
                s.add(Patient(chart_no=chart_no, name=label[:80], birth_date=birth,
                              last_visit_on=treated_on))
        if apply:
            await s.commit()
            print(f"  생성 완료 {len(missing)}명 — 이름에 '(연동)' 표기(합성 마스터)")
        else:
            for c in sorted(missing)[:5]:
                print(f"  · {c}")
            print("  … (DRY RUN — 반영하려면 --apply)")


if __name__ == "__main__":
    asyncio.run(main())
