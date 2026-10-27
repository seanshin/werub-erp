"""itam — IT 자산관리(ITAM) 도메인. 하드웨어 대장·배정·벤더 계약(M1).

import 시 첨부 무결성 검증(존재·폐기 삭제가드)을 common 레지스트리에 등록한다(의존성 역전).
"""
from erp.itam.attachment_guards import register as _register_attachment_guards

_register_attachment_guards()
