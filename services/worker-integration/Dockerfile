# worker-integration — erp 패키지 공유 (코드 단일 소스: services/core)
# 빌드 컨텍스트: services/ (compose 참조)
FROM python:3.12-slim
WORKDIR /app
COPY core/requirements.lock core/pyproject.toml ./
COPY core/src ./src
# 3서비스(core+워커2) 동일 락으로 재현·상호 정합(P1-6)
RUN pip install --no-cache-dir -r requirements.lock && \
    pip install --no-cache-dir --no-deps . && useradd -r -u 10002 erp
USER erp
CMD ["python", "-m", "erp.workers.integration_main"]
