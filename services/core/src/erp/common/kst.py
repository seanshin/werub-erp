"""KST(Asia/Seoul) 시각 헬퍼 — 프로세스 TZ(prod 컨테이너=UTC)에 의존하지 않는 일관된 로컬 날짜/시각.

배치 잡·마트 마감월 판정 등 병원 운영 시간대(KST) 기준이어야 하는 로직이 naive date.today()/
datetime.now()를 쓰면 컨테이너 TZ에 좌우된다. 특히 매월 1일 00:00~08:59 KST 구간엔 UTC가 아직
전월이라 "마감월" 판정이 뒤집혀(예: BI 마트 동결이 방금 마감된 월을 열린 월로 오판) 월 경계 오작동을
낳는다. 스케줄러 발화 TZ는 이미 Asia/Seoul로 고정돼 있으므로 잡 본문·집계도 동일 기준을 쓴다.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
_KST_OFFSET = timedelta(hours=9)


class ProcessTimezoneError(RuntimeError):
    """프로세스 로컬 TZ가 KST가 아님 — timestamptz 쓰기가 왜곡되므로 기동을 중단한다."""


def assert_process_tz_kst() -> None:
    """프로세스 로컬 TZ가 Asia/Seoul인지 검사(불일치 시 예외) — 엔트리포인트에서 호출.

    **왜 필요한가**: 이 코드베이스는 일부 컬럼(`groupware_attendance_staging.clock_in/out`,
    `recurring_task_runs.scheduled_for/started_at/finished_at`)에 **naive KST 벽시계**를
    `timestamptz`로 쓴다. asyncpg의 timestamptz 인코더는 naive 입력에 `obj.astimezone(utc)`를
    쓰는데, 이는 **쓰는 프로세스의 로컬 TZ**로 해석한다는 뜻이다. 즉 저장되는 '진짜 순간'이
    컨테이너 TZ에 좌우되고, TZ가 어긋나면 되읽기(`as_kst_wallclock`)가 ±9h 왜곡된다 —
    근태 야간·지각 판정과 급여 산정 입력, 정기업무 이상탐지가 조용히 전부 틀어진다.

    prod는 `infra/.env`의 `TZ=Asia/Seoul`에 의존해 왔다(2026-07-28 실측 정상). env 한 줄에
    매달린 불변식이라 Dockerfile ENV로 이중화하고, 그래도 어긋나면 **조용한 데이터 오염 대신
    기동 실패**를 택한다(적대검토 A-M2·B-F5·D 공통 지적).
    """
    off = datetime.now().astimezone().utcoffset()
    if off != _KST_OFFSET:
        raise ProcessTimezoneError(
            f"프로세스 TZ가 KST(+09:00)가 아님(현재 {off}) — TZ=Asia/Seoul 필요. "
            "naive KST를 timestamptz에 쓰는 경로가 왜곡되므로 기동을 중단한다.")


def kst_now() -> datetime:
    """현재 KST 시각(tz-aware)."""
    return datetime.now(KST)


def kst_today() -> date:
    """현재 KST 날짜 — 컨테이너 TZ 무관(월 경계 오판 방지)."""
    return datetime.now(KST).date()


def as_naive_wallclock(dt: datetime | None) -> datetime | None:
    """tzinfo만 떼어 벽시계 값을 보존(aware→naive), naive/None이면 그대로.

    ⚠ **DB(timestamptz) 되읽기 값에 쓰지 말 것** — asyncpg는 저장된 순간을 '진짜 UTC aware'로
    되돌려주므로(naive KST로 넣어도 세션 TZ 해석 후 UTC 환산·v1.257.1 prod 실측) 라벨만 떼면
    KST 대비 -9h 왜곡된다. 되읽기 정규화는 as_kst_wallclock 사용. 이 함수는 '이미 KST 벽시계인
    aware'(kst_now() 등 애플리케이션 생성 값)의 라벨 제거 전용."""
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo is not None else dt


def as_kst_wallclock(dt: datetime | None) -> datetime | None:
    """aware(진짜 순간)→KST로 환산한 벽시계 naive, naive/None이면 그대로 — DB 되읽기 정규화 전용.

    PG timestamptz는 asyncpg에서 UTC aware(진짜 순간)로 되읽힌다 → astimezone(KST) 후 라벨 제거가
    올바른 벽시계 복원. sqlite(naive 저장·KST 벽시계 관례)는 무변경 통과라 두 백엔드에서 동일 의미.
    """
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(KST).replace(tzinfo=None)


def kst_iso(dt: datetime | None) -> str | None:
    """datetime → KST isoformat 문자열(None 통과) — **수동 직렬화 지점 전용**.

    전역 인코더(`api_datetime_encoder`)는 raw datetime만 잡는다. 라우터/서비스가 응답 dict를
    만들며 직접 `.isoformat()`을 호출하면 그 필드는 **이미 문자열**이라 인코더 대상이 아니고,
    PG timestamptz 되읽기(UTC aware)가 그대로 `+00:00`으로 나간다 → 같은 응답 안에서 필드마다
    TZ가 다르고, FE가 `.slice(0, 10)`으로 자르는 화면은 **날짜가 하루 밀린다**(적대검토 D-H2).
    수동 직렬화가 불가피한 자리에서는 이 함수를 써 전역 인코더와 같은 값을 내보낸다.
    """
    return None if dt is None else api_datetime_encoder(dt)


def api_datetime_encoder(dt: datetime) -> str:
    """API 전역 datetime 직렬화 — aware(=PG timestamptz 되읽기)는 KST 환산 isoformat(+09:00).

    미등록 시 raw ORM datetime이 +00:00(UTC)으로 나가 FE의 문자열 절단 표시가 전 화면 -9h 왜곡
    (v1.259.1 광역 감사에서 확인 — 증명서 발급시각·기안 결재이력·사전심사 메모 등). naive(sqlite·
    수동 정규화 값)는 그대로. create_app이 fastapi ENCODERS_BY_TYPE[datetime]에 등록한다.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(KST).isoformat()
    return dt.isoformat()
