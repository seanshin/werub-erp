"""RBAC 정책 — 코드 분석 H1/H2/H3/M2/M4 회귀 방지 (read/write 분리·정책 차용 금지)."""
import pytest

from erp.common.policy import Role, ScopeKind, resolve_scope


def _scope(resource, action, role, dept=10, uid=1):
    return resolve_scope(resource, action, role=role, dept_id=dept, user_id=uid)


def test_prod_rejects_placeholder_secrets():
    """전체검토 — 프로덕션은 플레이스홀더 시크릿으로 기동 불가(토큰 위조·0키 PII 평문 방지·fail-closed)."""
    from erp.config import Settings
    prod = Settings(env="prod", jwt_secret="dev-only-change-me-needs-32-bytes-minimum!",
                    data_enc_key="0" * 64)
    with pytest.raises(RuntimeError, match="시크릿"):
        prod.load_secret_files()
    # dev는 플레이스홀더 허용(무예외)
    Settings(env="dev").load_secret_files()
    # prod라도 실 시크릿 주입 시 통과(phone_bidx 포함 — 공개 상수 가드)
    Settings(env="prod", jwt_secret="x" * 40, data_enc_key="a" * 64,
             phone_bidx_key="b" * 64).load_secret_files()


def test_workstation_excluded_from_regulatory_safety_and_master():
    """전체검토 — WORKSTATION(일일 출입증 kiosk)은 환자안전사고·규제마스터 접근 불가(enum-wide 부여 교정)."""
    for res, act in (("regulatory.safety", "read"), ("regulatory.safety", "write"),
                     ("regulatory.master", "read")):
        assert _scope(res, act, Role.WORKSTATION).kind == ScopeKind.NONE, (res, act)
        # 내부 직원(비-kiosk)은 여전히 허용(자율보고·참조 열람)
        assert _scope(res, act, Role.STAFF).kind == ScopeKind.ALL, (res, act)
        assert _scope(res, act, Role.MANAGER).kind == ScopeKind.ALL, (res, act)


def test_patient_write_is_admin_only():
    """분석 H1 — read 권한으로 환자 생성·수납이 뚫리던 결함 회귀 방지."""
    assert _scope("patient.patients", "write", Role.ADMIN).kind == ScopeKind.ALL
    assert _scope("patient.patients", "write", Role.MANAGER).kind == ScopeKind.NONE
    assert _scope("patient.patients", "write", Role.DIRECTOR).kind == ScopeKind.NONE
    assert _scope("patient.patients", "write", Role.STAFF).kind == ScopeKind.NONE


def test_patient_read_manager_dept_scoped():
    s = _scope("patient.patients", "read", Role.MANAGER, dept=7)
    assert s.kind == ScopeKind.DEPT and s.dept_id == 7


def test_patient_closing_separated_admin_only():
    """일마감을 수납 입력과 분리 — write/approve는 ADMIN 전용, read는 이력·미리보기용."""
    assert _scope("patient.closing", "write", Role.ADMIN).kind == ScopeKind.ALL
    assert _scope("patient.closing", "approve", Role.ADMIN).kind == ScopeKind.ALL
    for r in (Role.DIRECTOR, Role.MANAGER, Role.STAFF):
        assert _scope("patient.closing", "write", r).kind == ScopeKind.NONE
        assert _scope("patient.closing", "approve", r).kind == ScopeKind.NONE
    # 조회(이력·일계 미리보기)는 ALL 역할(원장·관리자) 전용 — history/preview가 전부서 합산이라
    # 부서장(DEPT)은 과다노출이 되어 제외(전체검토 결정).
    assert _scope("patient.closing", "read", Role.DIRECTOR).kind == ScopeKind.ALL
    assert _scope("patient.closing", "read", Role.ADMIN).kind == ScopeKind.ALL
    assert _scope("patient.closing", "read", Role.MANAGER, dept=3).kind == ScopeKind.NONE
    assert _scope("patient.closing", "read", Role.STAFF).kind == ScopeKind.NONE


def test_scm_items_read_separate_from_narcotics():
    """분석 H3/M2 — 일반 재고가 마약류 정책에 묶이지 않음."""
    assert _scope("scm.items", "read", Role.MANAGER).kind == ScopeKind.ALL
    assert _scope("scm.items", "read", Role.STAFF).kind == ScopeKind.NONE
    assert _scope("scm.items", "write", Role.MANAGER).kind == ScopeKind.DEPT


def test_narcotics_read_does_not_require_write():
    """분석 M2 — 수불부 조회가 쓰기 권한을 요구하지 않음."""
    assert _scope("scm.narcotics", "read", Role.DIRECTOR).kind == ScopeKind.ALL
    assert _scope("scm.narcotics", "write", Role.DIRECTOR).kind == ScopeKind.NONE


def test_finance_budgets_dedicated_policy():
    """분석 M4 — hr.duties 차용 제거: 전용 finance.budgets read."""
    assert _scope("finance.budgets", "read", Role.MANAGER, dept=3).kind == ScopeKind.DEPT
    assert _scope("finance.budgets", "read", Role.STAFF).kind == ScopeKind.NONE


def test_unregistered_resource_denied():
    """매트릭스 미등재 = 거부 (기본 거부 원칙)."""
    assert _scope("no.such", "read", Role.ADMIN).kind == ScopeKind.NONE


def test_require_scope_raises_forbidden():
    from erp.errors import ForbiddenError

    class FakeUser:
        role, dept_id, user_id = "STAFF", None, 1

    from erp.common.policy import require_scope
    with pytest.raises(ForbiddenError):
        require_scope(FakeUser(), "finance.vouchers", "write")

def test_finance_periods_registered():
    """보안 점검 — finance.periods 미등록 403 회귀 방지."""
    assert _scope("finance.periods", "read", Role.ADMIN).kind == ScopeKind.ALL
    assert _scope("finance.periods", "write", Role.ADMIN).kind == ScopeKind.ALL
    assert _scope("finance.periods", "write", Role.MANAGER).kind == ScopeKind.NONE


def test_finance_budgets_write_director_admin():
    assert _scope("finance.budgets", "write", Role.ADMIN).kind == ScopeKind.ALL
    assert _scope("finance.budgets", "write", Role.DIRECTOR).kind == ScopeKind.ALL
    assert _scope("finance.budgets", "write", Role.STAFF).kind == ScopeKind.NONE


def test_departments_read_gated_manager_plus():
    """보안스윕 MED-1 — 조직구조 조회는 매니저+ (STAFF·WORKSTATION 차단)."""
    for r in (Role.DIRECTOR, Role.ADMIN, Role.MANAGER):
        assert _scope("common.departments", "read", r).kind == ScopeKind.ALL
    assert _scope("common.departments", "read", Role.STAFF).kind == ScopeKind.NONE
    # 쓰기는 ADMIN 전용 유지
    assert _scope("common.departments", "write", Role.ADMIN).kind == ScopeKind.ALL
    assert _scope("common.departments", "write", Role.MANAGER).kind == ScopeKind.NONE


def test_compliance_read_admin_director_only():
    """컴플라이언스 만기 대시보드 — 전사 크로스도메인(면허·계약·방사선 피폭선량=민감 건강정보)
    이라 부서장(DEPT 우회) 전사 노출 차단, 원장·관리자 전용(성능·보안검토 2026-07-04)."""
    assert _scope("regulatory.compliance", "read", Role.DIRECTOR).kind == ScopeKind.ALL
    assert _scope("regulatory.compliance", "read", Role.ADMIN).kind == ScopeKind.ALL
    for r in (Role.MANAGER, Role.STAFF, Role.WORKSTATION):
        assert _scope("regulatory.compliance", "read", r).kind == ScopeKind.NONE


def test_domain_manager_roles_scoped_to_domain():
    """도메인 관리자(요청·#5) — 자기 도메인 read/write ALL, 타 도메인·시스템 관리 불가."""
    # IT관리자 — itam 전권, finance/scm 불가, 사용자관리 불가
    assert _scope("itam.assets", "write", Role.IT_MANAGER).kind == ScopeKind.ALL
    assert _scope("itam.assets", "read", Role.IT_MANAGER).kind == ScopeKind.ALL
    assert _scope("finance.vouchers", "write", Role.IT_MANAGER).kind == ScopeKind.NONE
    assert _scope("scm.items", "write", Role.IT_MANAGER).kind == ScopeKind.NONE
    assert _scope("admin.users", "write", Role.IT_MANAGER).kind == ScopeKind.NONE
    # 회계관리자 — finance/세무/원가 write, 단 SoD로 기간마감·현금전기 write는 제외(ADMIN 유지·재검토 HIGH)
    for res in ("finance.vouchers", "finance.statements", "finance.taxinvoice",
                "finance.accounts", "finance.budgets", "finance.assets"):
        assert _scope(res, "write", Role.FINANCE_MANAGER).kind == ScopeKind.ALL
    for res in ("finance.periods", "finance.settlement"):        # 마감·현금전기 write 불가
        assert _scope(res, "write", Role.FINANCE_MANAGER).kind == ScopeKind.NONE
        assert _scope(res, "read", Role.FINANCE_MANAGER).kind == ScopeKind.ALL   # 조회는 가능
    assert _scope("analytics.reports", "read", Role.FINANCE_MANAGER).kind == ScopeKind.ALL
    assert _scope("itam.assets", "write", Role.FINANCE_MANAGER).kind == ScopeKind.NONE
    assert _scope("admin.users", "write", Role.FINANCE_MANAGER).kind == ScopeKind.NONE
    # 물류관리자 — 재고·발주 write, 단 마약류 write 제외(마약류관리법·재검토 MED)·조회는 마약류 포함
    for res in ("scm.items", "scm.po"):
        assert _scope(res, "write", Role.LOGISTICS_MANAGER).kind == ScopeKind.ALL
    assert _scope("scm.narcotics", "write", Role.LOGISTICS_MANAGER).kind == ScopeKind.NONE
    assert _scope("scm.narcotics", "read", Role.LOGISTICS_MANAGER).kind == ScopeKind.ALL
    assert _scope("finance.vouchers", "write", Role.LOGISTICS_MANAGER).kind == ScopeKind.NONE
    assert _scope("itam.assets", "write", Role.LOGISTICS_MANAGER).kind == ScopeKind.NONE
    # 공통: 부서 조회·결재 참여 / 결재선 룰 설정은 불가(ADMIN 유지)
    for r in (Role.IT_MANAGER, Role.FINANCE_MANAGER, Role.LOGISTICS_MANAGER):
        assert _scope("common.departments", "read", r).kind == ScopeKind.ALL
        assert _scope("common.approvals", "write", r).kind == ScopeKind.ALL
        assert _scope("common.approval_rules", "write", r).kind == ScopeKind.NONE
