# beets 2.14.1 supports Python < 3.15, so this stays on 3.14 until beets catches up.
FROM python:3.14.10-slim-trixie

# chromaprint + ffmpeg let beets identify untagged files by their audio fingerprint
# (pyacoustid decodes through ffmpeg when the chromaprint library is present).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libchromaprint-tools ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir --no-deps .

ENV PYTHONUNBUFFERED=1 \
    MUSIC_DIR=/music \
    DOWNLOADS_DIR=/downloads \
    DATA_DIR=/data \
    HOME=/data

USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
ENTRYPOINT ["music-importer"]
CMD ["serve"]
