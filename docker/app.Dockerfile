# NIRNAY single-container image: API + embedded camera workers/ingestion/scheduler + the built
# console, all on one port. Intended for PaaS hosts that run one web service per repo
# (Railway, Render, Koyeb, a single VM). The multi-service Compose stack is docker-compose.yml.
#
#   docker build -f docker/app.Dockerfile -t nirnay .
#   docker run -p 8000:8000 -v nirnay-data:/data -e DEMO_PASSWORD=<strong> nirnay
FROM node:22-alpine AS console
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.13-slim
# MALLOC_ARENA_MAX: glibc otherwise creates one malloc arena per busy thread, which costs
# ~250 MB of fragmentation across the camera/inference threads.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 MALLOC_ARENA_MAX=2 \
    DATA_DIR=/data MODELS_DIR=/app/models FRONTEND_DIR=/app/frontend/dist WORKER_MODE=embedded
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt
COPY scripts /app/scripts
COPY models /app/models
# models are fetched at build time with pinned SHA-256 checksums (see models/README.md)
ARG DOWNLOAD_MODELS=1
RUN if [ "$DOWNLOAD_MODELS" = "1" ]; then python /app/scripts/download_models.py || echo "model download failed: fallbacks will be used"; fi
COPY configs /app/configs
COPY backend /app/backend
COPY --from=console /src/dist /app/frontend/dist
COPY docker/start.sh /app/start.sh
RUN chmod 755 /app/start.sh && useradd --system --uid 10001 --home /app nirnay && mkdir -p /data && chown -R nirnay /data
USER nirnay
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/health" || exit 1
CMD ["/app/start.sh"]
