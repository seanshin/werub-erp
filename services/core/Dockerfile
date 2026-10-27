# core 서비스 — 비root 실행, entrypoint에서 마이그레이션 자동 (design §11.0)
FROM python:3.12-slim

# WeasyPrint 런타임 (pango·cairo) + 한글 폰트 — 격리망: 빌드 시 포함 (§6.7 외부 fetch 금지)
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
      fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/*

# 프로세스 TZ 고정 — naive KST를 timestamptz에 쓰는 경로(근태 clock·정기업무 실행이력)의
# 저장 순간이 프로세스 로컬 TZ로 해석되므로 KST가 아니면 ±9h 왜곡된다(assert_process_tz_kst).
ENV TZ=Asia/Seoul

WORKDIR /app
COPY requirements.lock pyproject.toml ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
# 락파일로 재현 가능한 설치(P1-6) — 고정 버전 설치 후 앱은 --no-deps로(재해석 방지).
RUN pip install --no-cache-dir -r requirements.lock && \
    pip install --no-cache-dir --no-deps . && \
    useradd -r -u 10001 erp && \
    mkdir -p /data/attachments && chown -R 10001:10001 /data && \
    chmod 2775 /data/attachments   # worker-batch(그룹 10001)와 공유 — 서명 완료본 회수
USER erp

# 마이그레이션(advisory lock으로 동시 기동 방어) 후 서버 기동
CMD ["sh", "-c", "python -m erp.migrate_on_start && uvicorn erp.main:app --host 0.0.0.0 --port 8000"]
