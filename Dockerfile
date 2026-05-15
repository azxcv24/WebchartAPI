# --- Build stage ------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir --prefix=/install .

# --- Runtime stage ----------------------------------------------------------
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system webchart \
    && useradd --system --gid webchart --home /app --shell /usr/sbin/nologin webchart \
    && mkdir -p /data/webchart/uploads /data/webchart/datasets \
    && chown -R webchart:webchart /app /data/webchart

COPY --from=builder /install /usr/local

USER webchart

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "webchart.main:app", "--host", "0.0.0.0", "--port", "8000"]
