FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/app/data/catalog.db \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# Optional speedups: TgCrypto makes MTProto encryption ~10x faster, uvloop speeds
# up the event loop. Best-effort so a missing wheel never breaks the build.
RUN pip install tgcrypto uvloop || echo "optional speedups skipped"

COPY app ./app

VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["python", "-m", "app.main"]
