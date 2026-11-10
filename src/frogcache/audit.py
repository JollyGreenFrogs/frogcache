"""Audit log (ADR-004): every frogcache action written to a SQLite DB on the data share.

Privacy rule: coarse categories only — never response bodies, tokens, or
file contents. Env: AUDIT_DB (default /data/audit.db).

Governing ADR: docs/decisions/ADR-004.
"""

import json
from datetime import datetime, timezone



def log_event(
    conn,
    event: str,
    artifact: str = "",
    version: str = "",
    **detail,
) -> None:
    """Append one audit event.

    Args:
        conn: connection from connect().
        event: event type, e.g. 'check_start', 'verify_fail', 'promote'.
        artifact: artifact name (coarse identifier only).
        version: version/revision/sha.
        **detail: extra fields, JSON-encoded (sizes, digest_verified, counts —
            never bodies/tokens/contents).
    """
    conn.execute(
        "INSERT INTO events (ts, event, artifact, version, detail) VALUES (%(ts)s, %(event)s, %(artifact)s, %(version)s, %(detail)s)",
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "artifact": artifact,
            "version": version,
            "detail": json.dumps(detail, sort_keys=True),
        },
    )
    conn.commit()


def recent_events(conn, limit: int = 100) -> list[dict]:
    """Read the newest events (for the `--audit` CLI flag / debugging).

    Args:
        conn: connection from connect().
        limit: max rows.
    Returns:
        List of event dicts, newest first.
    """
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT %(limit)s", {"limit": limit}
    ).fetchall()
    return rows
