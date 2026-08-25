FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NOSTALGIA_CONFIG=/config/config.yaml \
    NOSTALGIA_HOST=0.0.0.0 \
    NOSTALGIA_PORT=8777

WORKDIR /app

# gosu drops privileges for PUID/PGID; tzdata makes the TZ env var mean something.
RUN apt-get update && apt-get install -y --no-install-recommends gosu tzdata && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nostalgia_line/ ./nostalgia_line/
COPY web/ ./web/
COPY data/ ./data/
COPY run.py docker/entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Everything the user owns - config, their channels.csv, overrides, exports and
# the TMDB cache - lives here. Mount it to keep it across upgrades.
VOLUME ["/config"]
EXPOSE 8777

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c 'import os,urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.environ.get("NOSTALGIA_PORT","8777") + "/api/status", timeout=4)' || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "run.py"]
