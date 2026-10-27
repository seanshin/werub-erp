"""근태 판정용 시프트 시작/종료 시각 — shift_types 카탈로그 확장 (FR-32/FR-29).

근태 지각·조퇴·야간 판정(hr/attendance)이 하드코딩 SHIFT_TIMES(D/E/N)만 알아 인사팀이
등록한 커스텀 시프트(당직 OC 등)는 무판정이었다. shift_types에 start_time·end_time을 추가해
판정이 카탈로그를 SoT로 쓰도록(미설정 시 코드 기본값 폴백 — 하위호환).

nullable 컬럼 추가만 — 기존 행(D/E/N/OFF) 무영향, downgrade에서 drop.
참조: 전체검토 2026-06-27 항목 4.
"""
import sqlalchemy as sa
from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shift_types", sa.Column("start_time", sa.Time(), nullable=True),
                  schema="hr")
    op.add_column("shift_types", sa.Column("end_time", sa.Time(), nullable=True),
                  schema="hr")


def downgrade() -> None:
    op.drop_column("shift_types", "end_time", schema="hr")
    op.drop_column("shift_types", "start_time", schema="hr")
