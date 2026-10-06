FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config ./config
COPY scripts ./scripts
COPY config.ini ./

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data /app/materials \
    && chown -R app:app /app
USER app

# the app writes data/heartbeat.json every minute; unhealthy if it is stale or polling stopped
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD python -m src.health check

CMD ["python", "-m", "src.main"]
