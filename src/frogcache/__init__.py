"""frogcache — self-hosted LAN artifact cache with weekly auto-update.

Serves cached downloads (wheels, release assets, ...) over a flat HTTP index
(nginx autoindex, served by the `cache` container in deploy/) and runs an
`updater` container that checks each configured artifact's upstream once a
week, downloads newer versions into the cache, and retains the latest +
latest-1 files per artifact. Notifications go to ntfy.

Invariants: stdlib only; functions over classes; models live in data_models/;
runtime config is env-only; secrets come from a KEY=VALUE file mounted at
/run/secrets (never committed). Governing ADR: docs/decisions/ADR-001.
"""
