# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY src/bosshunter/web/frontend/package*.json ./
RUN npm ci
COPY src/bosshunter/web/frontend/ ./
RUN npm run build

FROM python:3.11-slim-bookworm AS wheel
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY --from=frontend /build/frontend/dist/ ./src/bosshunter/web/frontend/dist/
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.11-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOSSHUNTER_CONTAINER=true \
    BOSSHUNTER_TRUST_LOCAL_PORT_PUBLISH=true \
    BOSSHUNTER_HOST=0.0.0.0 \
    BOSSHUNTER_PORT=8686 \
    BOSSHUNTER_BASE_DIR=/app/runtime \
    BOSSHUNTER_CONFIG_PATH=/app/runtime/config.yaml \
    BOSSHUNTER_MIGRATIONS_DIR=/app/migrations/postgresql \
    BOSSHUNTER_BROWSER_RUNTIME=external \
    BOSSHUNTER_BROWSER_AUTO_START_PROXY=false
RUN groupadd --system bosshunter && useradd --system --gid bosshunter --home-dir /app bosshunter
WORKDIR /app
COPY --from=wheel /wheels/ /wheels/
RUN python -m pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
COPY --chown=bosshunter:bosshunter migrations/ /app/migrations/
COPY --chown=bosshunter:bosshunter docker/ /app/docker/
RUN mkdir -p /app/runtime/data && chown -R bosshunter:bosshunter /app
USER bosshunter
EXPOSE 8686
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=4 CMD ["python", "/app/docker/healthcheck.py"]
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
