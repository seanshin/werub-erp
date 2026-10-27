# worker-batch — erp 패키지 공유 (코드 단일 소스: services/core)
# 빌드 컨텍스트: services/ (compose 참조)
FROM python:3.12-slim

# pg_dump (백업 잡 §6.5)·openssh-client+rsync (오프사이트 백업 전송 P2-1·시스템 바이너리).
# base=Debian trixie → postgresql-client=17 (PG16 서버 덤프 가능)
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client openssh-client rsync && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY core/requirements.lock core/pyproject.toml ./
COPY core/src ./src
# 3서비스(core+워커2) 동일 락으로 재현·상호 정합(P1-6)
RUN pip install --no-cache-dir -r requirements.lock && \
    pip install --no-cache-dir --no-deps . && useradd -r -u 10003 erp && \
    mkdir -p /data/backup /data/notice-drop /data/exports && chown -R 10003:10003 /data
USER erp
CMD ["python", "-m", "erp.workers.scheduler"]
