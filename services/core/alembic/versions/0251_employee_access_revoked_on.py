"""0251 — 퇴사자 ERP 계정 차단 1회 표식(hr.employees.access_revoked_on).

v1.288 흐름 검토(G9): 일일 차단 잡이 퇴사일이 지난 직원 전체를 매번 차단해, 관리자가 일부러 다시 켠
계정(자문·재계약)을 다음 날 또 막았다. 그 퇴사(resigned_on)에 대해 **한 번만** 차단하고 시점을 남긴다.
NULL인 과거 퇴사자는 배포 후 첫 실행이 일괄 차단한다(따라잡기).

Revision ID: 0251
Revises: 0250
"""
import sqlalchemy as sa
from alembic import op

revision = "0251"
down_revision = "0250"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("access_revoked_on", sa.Date(), nullable=True),
                  schema="hr")
    import json
    op.get_bind().execute(sa.text(
        "INSERT INTO common.pending_decisions (key, category, title, question, options, context_ref) "
        "VALUES (:k, :c, :t, :q, :o, :r) ON CONFLICT (key) DO NOTHING"), {
        "k": "resigned-self-service-access", "c": "SECURITY",
        "t": "퇴사자의 급여명세서·원천징수영수증 열람 경로",
        "q": "v1.288부터 퇴사일 다음 날 ERP 계정이 차단되어, ERP 셀프서비스(내 급여명세·원천징수영수증) 열람도 "
             "함께 끊깁니다. HIS ESS 경로(정적 연동키)는 영향이 없습니다. 퇴사자의 증명서·명세서 발급 경로를 정해야 "
             "합니다(근로기준법상 퇴직자 요청 시 교부 의무).",
        "o": json.dumps([
            {"key": "HR_ISSUE", "label": "인사팀 발급(현행)",
             "description": "퇴사자는 요청 → 인사팀이 ERP에서 발급·송부", "recommended": True},
            {"key": "HIS_ESS", "label": "HIS ESS로 열람",
             "description": "HIS 계정이 유지되는 기간 동안 ESS로 열람(HIS 정책에 따름)"},
            {"key": "LIMITED_ACCOUNT", "label": "열람 전용 계정 유지",
             "description": "퇴사 후 N개월 열람 전용 역할 — 개발 필요"}], ensure_ascii=False),
        "r": "services/core/src/erp/hr/access_revoke.py"})
    op.get_bind().execute(sa.text(
        "INSERT INTO common.pending_decisions (key, category, title, question, options, context_ref) "
        "VALUES (:k, :c, :t, :q, :o, :r) ON CONFLICT (key) DO NOTHING"), {
        "k": "his-rehire-employee-number", "c": "INTEGRATION",
        "t": "HIS 재입사 표현 — 같은 사번 재사용인가, 새 사번인가",
        "q": "ERP는 HIS staff 이벤트에서 퇴사일(resigned_on)을 지우지 않는다. HIS가 재입사를 **같은 사번**으로 "
             "다시 보내면 ERP 직원 기록은 퇴사 상태로 남아 계정이 계속 차단되고 재입사 후보도 생기지 않는다. "
             "새 사번이면 ERP가 재입사 후보를 관리자에게 알리고 화면에서 재연결한다(v1.288). HIS 계약 확인 필요.",
        "o": json.dumps([
            {"key": "NEW_NUMBER", "label": "새 사번(현행 ERP 처리와 맞음)",
             "description": "확인만 — ERP 변경 없음", "recommended": True},
            {"key": "SAME_NUMBER", "label": "같은 사번 재사용",
             "description": "ERP가 HIS의 재직 상태 이벤트에서 resigned_on을 지우고 재입사 이력을 남기도록 개발"}],
            ensure_ascii=False),
        "r": "services/core/src/erp/integrations/inbound_consumer.py _staff"})


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM common.pending_decisions WHERE key IN ('resigned-self-service-access', "
        "'his-rehire-employee-number') "
        "AND status='PENDING'"))
    op.drop_column("employees", "access_revoked_on", schema="hr")
