"""0010 — 표준 계정과목 보강 (서비스 기본 계정 5110 진료비용 등 — 재무제표 집계 정합).

code-analyzer 검증 중 재무상태표 balanced=False로 발견: SettlementService 기본 비용계정
5110이 계정 마스터에 없어(시드 누락) 전표 라인이 손익/재무제표 집계에서 탈락 → 자산=부채+자본
불일치. 마스터 보강 시 기존 전표 라인도 조인되어 정합 회복.

Revision ID: 0010
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES
            ('5110','진료비용','EXPENSE'),
            ('5310','급여비용','EXPENSE'),
            ('5510','관리비용','EXPENSE')
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code IN ('5310','5510')")
    # 5110은 전표에서 참조 중일 수 있어 보존
