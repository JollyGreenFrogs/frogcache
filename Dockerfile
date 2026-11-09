# frogcache updater image — stdlib-only, so no dependency resolution is needed
# at build or run time (works even when external hosts are unreachable).
FROM python:3.15-alpine

WORKDIR /app
COPY src ./src
ENV PYTHONPATH=/app/src \
    CACHE_DIR=/cache \
    ARTIFACTS_FILE=/artifacts/artifacts.json \
    STATE_FILE=/data/manifest.json \
    SECRETS_FILE=/run/secrets/frogcache.env \
    CHECK_INTERVAL_SECONDS=604800 \
    RETAIN=2

CMD ["python", "-m", "frogcache", "--loop"]
