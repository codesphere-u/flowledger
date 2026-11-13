FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8095

WORKDIR /app

RUN groupadd --system appuser && useradd --system --gid appuser appuser

COPY pyproject.toml README.md ./
COPY flowledger ./flowledger

RUN pip install --no-cache-dir . && chown -R appuser:appuser /app

USER appuser

EXPOSE 8095

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8095\")}/healthz', timeout=3)" || exit 1

CMD ["sh", "-c", "exec flowledger serve --host 0.0.0.0 --port ${PORT:-8095}"]
