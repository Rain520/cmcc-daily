FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    CMCC_DATA_DIR=/data \
    CMCC_PORT=8000

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 cmcc \
    && useradd --uid 10001 --gid cmcc --no-create-home --shell /usr/sbin/nologin cmcc \
    && mkdir -p /app /data/cache /data/logs /data/runtime \
    && chown -R cmcc:cmcc /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY cmcc_*.py ./
COPY cmcc_app/ ./cmcc_app/

LABEL org.opencontainers.image.title="CMCC Daily" \
      org.opencontainers.image.description="Self-hosted CMCC check-in dashboard based on yupaiLy/cmcc-auto-checkin"

USER 10001:10001
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD ["python", "-m", "cmcc_app", "healthcheck"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "cmcc_app"]
CMD ["serve"]
