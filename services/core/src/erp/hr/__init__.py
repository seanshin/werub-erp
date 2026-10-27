"""HR 도메인 패키지 — import 시 첨부 행 수준 가드를 common 레지스트리에 등록."""
from erp.hr.attachment_guards import register as _register_attachment_guards

_register_attachment_guards()
