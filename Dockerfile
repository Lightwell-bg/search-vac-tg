FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
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

CMD ["python", "-m", "src.main"]
