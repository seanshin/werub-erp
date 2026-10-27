"""patient — 원무(접수·수납·일마감·본인부담 발생주의) 도메인.

import 시 환자 AR(1131) as_of 수금 재구성기를 finance 정산 레지스트리에 등록한다(의존성 역전:
finance는 patient를 import 못 하므로 회수 소스를 아는 patient가 자신의 재구성기를 주입).
이로써 대손충당(IFRS 9 ECL)의 reconstruct=True aging이 환자 본인부담 채권에도 시점 정확해진다.
"""
from erp.finance.settlement_service import register_ar_collected_reconstructor
from erp.patient.receivable_service import collected_as_of as _patient_ar_collected_as_of

register_ar_collected_reconstructor("patient", _patient_ar_collected_as_of)
