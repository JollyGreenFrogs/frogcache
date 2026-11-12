# frogcache updater image — python + clamav (scan-on-fetch, ADR-003 §D) +
# alembic/sqlalchemy/psycopg for the Postgres store (ADR-004).
FROM python:3.15-alpine
RUN apk add --no-cache clamav \
    && pip install --no-cache-dir "alembic>=1.13" "sqlalchemy>=2.0" "psycopg[binary]>=3.2"

WORKDIR /app
COPY src ./src
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
ENV PYTHONPATH=/app/src \
    CACHE_DIR=/cache \
    ARTIFACTS_FILE=/artifacts/artifacts.json \
    STATE_FILE=/data/manifest.json \
    SECRETS_FILE=/run/secrets/frogcache.env \
    CHECK_INTERVAL_SECONDS=604800 \
    RETAIN=2

CMD ["sh", "-c", "freshclam --quiet || true; exec python -m frogcache --loop"]
