"""RBAC 매트릭스 스냅샷 (P1-2) — POLICY 변경을 강제 리뷰로.

WORKSTATION kiosk 과다부여(v1.71 escape: enum-wide 부여가 regulatory.safety/master를 뚫음)
같은 사고를 방지. POLICY dict 어느 grant든 바뀌면 스냅샷이 깨져 리뷰를 강제한다.
정책 변경이 의도적이면 아래 갱신 명령으로 스냅샷을 재생성하고 diff를 리뷰에 첨부한다:
  python -c "from tests.test_rbac_matrix import write_snapshot; write_snapshot()"
"""
import pathlib

from erp.common.policy import POLICY, Role, ScopeKind

_SNAPSHOT = pathlib.Path(__file__).parent / "rbac_matrix.snapshot"
_ROLE_ORDER = [r.value for r in Role]


def _matrix_text() -> str:
    """결정적 매트릭스 텍스트 — (resource.action: ROLE=SCOPE …) 정렬. NONE은 생략."""
    lines = []
    for res, act in sorted(POLICY):
        grants = POLICY[(res, act)]
        parts = [f"{role}={grants[Role(role)].value}" for role in _ROLE_ORDER
                 if grants.get(Role(role), ScopeKind.NONE) != ScopeKind.NONE]
        lines.append(f"{res}.{act}: " + " ".join(parts))
    return "\n".join(lines) + "\n"


def write_snapshot() -> None:
    _SNAPSHOT.write_text(_matrix_text())


def test_rbac_matrix_matches_snapshot():
    """POLICY 매트릭스가 커밋된 스냅샷과 일치 — 어떤 grant 변경도 리뷰 강제."""
    current = _matrix_text()
    committed = _SNAPSHOT.read_text()
    assert current == committed, (
        "RBAC POLICY가 스냅샷과 다릅니다. 의도적 변경이면 write_snapshot()으로 재생성 후 "
        "diff를 리뷰에 첨부하세요(권한 확대는 특히 주의).")


def test_workstation_confined_to_scm():
    """WORKSTATION(일일 출입증 kiosk·비직원)은 scm.* 외 어떤 리소스도 접근 불가.

    enum-wide 부여(footgun)로 kiosk가 환자안전·규제마스터·재무 등을 쓸어담던 회귀(v1.71) 방지."""
    leaks = [(res, act) for (res, act), grants in POLICY.items()
             if grants.get(Role.WORKSTATION, ScopeKind.NONE) != ScopeKind.NONE
             and not res.startswith("scm.")]
    assert not leaks, f"WORKSTATION이 scm 외 리소스 접근 허용됨(kiosk 과다부여): {leaks}"


def test_no_role_has_empty_string_or_unknown_scope():
    """정합성 — 모든 grant 값은 유효한 ScopeKind, 역할은 유효한 Role."""
    for (res, act), grants in POLICY.items():
        for role, kind in grants.items():
            assert isinstance(role, Role), (res, act, role)
            assert isinstance(kind, ScopeKind), (res, act, role, kind)


def test_all_router_scope_refs_registered():
    """라우터의 3-arg require_scope/_require(user, "res.x", "action") 리터럴이 전부 POLICY에 등록.

    미등록 (resource, action)은 default-deny로 전역 403이 되던 결함(예: finance.periods 미등록)을
    코드 스캔으로 사전 적발."""
    import re

    pat = re.compile(
        r'(?:require_scope|_require)\([^)]*?"([a-z_]+\.[a-z_]+)"\s*,\s*"(read|write|approve|export)"')
    src = pathlib.Path(__file__).parent.parent / "src" / "erp"
    refs = set()
    for f in src.rglob("*.py"):
        for m in pat.finditer(f.read_text()):
            refs.add((m.group(1), m.group(2)))
    assert refs, "스캔이 아무 참조도 못 찾음 — 정규식/경로 확인"
    missing = sorted(r for r in refs if r not in POLICY)
    assert not missing, f"POLICY 미등록 (resource, action) — default-deny 403 위험: {missing}"
