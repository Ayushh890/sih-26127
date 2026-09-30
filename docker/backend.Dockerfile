# NIRNAY backend + worker image (the same image runs the API and the processing worker).
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data MODELS_DIR=/app/models
# libgl/glib are needed by OpenCV's video I/O; ffmpeg libraries ship inside the opencv wheel
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt
COPY backend /app/backend
COPY configs /app/configs
COPY scripts /app/scripts
COPY models /app/models
# Download any model that is not already present in ./models (checksums are verified).
# Build with --build-arg DOWNLOAD_MODELS=0 for offline builds: the pipeline then uses the
# documented fallbacks (motion detector, contour plate finder, colour-histogram Re-ID).
ARG DOWNLOAD_MODELS=1
RUN if [ "$DOWNLOAD_MODELS" = "1" ]; then python /app/scripts/download_models.py || echo "model download failed: fallbacks will be used"; fi
RUN useradd --system --uid 10001 --home /app nirnay && mkdir -p /data && chown -R nirnay /data
USER nirnay
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 CMD curl -fsS http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
