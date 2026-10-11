FROM python:3.12-slim

LABEL org.opencontainers.image.title="ScrapeFlow" \
      org.opencontainers.image.description="Self-hosted watcher that mirrors files from websites into your storage" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SCRAPEFLOW_DATA_DIR=/data

COPY pyproject.toml README.md LICENSE ./
COPY scrapeflow ./scrapeflow
RUN pip install ".[s3]" \
    && useradd --create-home --uid 10001 scrapeflow \
    && mkdir -p /data /downloads && chown scrapeflow:scrapeflow /data /downloads

USER scrapeflow
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"

CMD ["scrapeflow", "serve"]
