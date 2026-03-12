FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY src/ src/

RUN mkdir -p /data/webchart/uploads /data/webchart/datasets

EXPOSE 8000

CMD ["uvicorn", "webchart.main:app", "--host", "0.0.0.0", "--port", "8000"]
