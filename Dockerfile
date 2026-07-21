FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/vogelradar

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /data holds the SQLite database (persistent volume) and, in production,
# the mounted firebase-service-account.json.
RUN useradd --system --no-create-home vogelradar \
    && mkdir -p /data \
    && chown vogelradar /data
USER vogelradar

ENV VOGELRADAR_DB_PATH=/data/vogelradar.db \
    FIREBASE_CREDENTIALS=/data/firebase-service-account.json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/birds.json', timeout=4)"

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
