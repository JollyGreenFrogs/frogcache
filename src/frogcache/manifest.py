"""Fetch-state store (ADR-003 §C / ADR-004): Postgres `fetches` table replaces manifest.json.

One row per fetched file; retention prunes rows so only the newest `retain`
versions per artifact remain. A legacy manifest.json (STATE_FILE env, or
manifest.json next to the DB) is imported once on first use and then left
untouched. Split from cache.py (file operations) per the house rule.

Governing ADRs: ADR-001 (wheels), ADR-002 (HF), ADR-003, ADR-004.
"""

import json
from pathlib import Path

from frogcache.net import redact_url

from frogcache.data_models.artifact import ManifestEntry

_COLS = "artifact, version, filename, sha256, source, url, fetched_at, digest_verified"


def entries(conn, name: str) -> list[dict]:
    """All recorded fetch entries for an artifact.

    Args:
        conn: DB connection from db.connect().
        name: artifact name.
    Returns:
        List of entry dicts (newest version first).
    """
    rows = conn.execute(
        f"SELECT {_COLS} FROM fetches WHERE artifact = %(name)s ORDER BY fetched_at DESC, id DESC",
        {"name": name},
    ).fetchall()
    return rows


def versions(conn, name: str) -> list[str]:
    """Distinct versions recorded for an artifact, newest first (by fetched_at).

    Args:
        conn: DB connection.
        name: artifact name.
    Returns:
        Version strings, newest first.
    """
    rows = conn.execute(
        "SELECT version, MAX(fetched_at) AS last FROM fetches WHERE artifact = %(name)s "
        "GROUP BY version ORDER BY last DESC",
        {"name": name},
    ).fetchall()
    return [row["version"] for row in rows]


def _prune_to_keep(conn, name: str, keep: int) -> list[str]:
    """Delete fetch rows whose version is older than the newest `keep` versions.

    Returns:
        Removed version strings.
    """
    kept = set(versions(conn, name)[:keep])
    all_versions = set(versions(conn, name))
    removed = sorted(all_versions - kept)
    if removed:
        marks = ",".join("%s" for _ in removed)
        conn.execute(
            f"DELETE FROM fetches WHERE artifact = %(name)s AND version = ANY(%(removed)s)",
            {"name": name, "removed": removed},
        )
        conn.commit()
    return removed


def record_fetch(conn, name: str, entry: ManifestEntry, keep: int) -> None:
    """Insert one fetch entry, then prune entries beyond the newest `keep` versions.

    Args:
        conn: DB connection.
        name: normalized artifact name.
        entry: ManifestEntry for the fetch.
        keep: number of newest versions to retain for the artifact.
    """
    conn.execute(
        f"INSERT INTO fetches ({_COLS}) VALUES (%(artifact)s, %(version)s, %(filename)s, %(sha256)s, %(source)s, %(url)s, %(fetched_at)s, %(digest_verified)s)",
        {"artifact": name, "version": entry.version, "filename": entry.filename,
         "sha256": entry.sha256, "source": entry.source, "url": redact_url(entry.url),
         "fetched_at": entry.fetched_at, "digest_verified": entry.digest_verified},
    )
    conn.commit()
    _prune_to_keep(conn, name, keep)


def record_fetch_multi(
    conn,
    name: str,
    version: str,
    files: list[tuple[str, str, bool]],
    source: str,
    fetched_at: str,
) -> None:
    """Insert one fetch row per downloaded file (multi-file fetch, ADR-002).

    Only successful fetches are recorded, so recorded versions == successful
    revisions; pruning happens separately via prune_versions so disk and DB
    retention can use the same keep-set.

    Args:
        conn: DB connection.
        name: artifact name.
        version: version (revision sha) all files belong to.
        files: [(filename, sha256, digest_verified)] triples.
        source: source type label.
        fetched_at: ISO-8601 UTC timestamp for the new rows.
    """
    for row in [
        # psycopg3 has no executemany — loop

            {"artifact": name, "version": version, "filename": filename, "sha256": sha256,
             "source": source, "url": "", "fetched_at": fetched_at, "digest_verified": verified}
            for filename, sha256, verified in files
    ]:
        conn.execute(
            f"INSERT INTO fetches ({_COLS}) VALUES (%(artifact)s, %(version)s, %(filename)s, %(sha256)s, %(source)s, %(url)s, %(fetched_at)s, %(digest_verified)s)",
            row,
        )
    conn.commit()


def prune_versions(conn, name: str, keep_versions: set[str]) -> list[str]:
    """Delete fetch rows whose version is not in keep_versions.

    Args:
        conn: DB connection.
        name: artifact name.
        keep_versions: versions to keep.
    Returns:
        Removed version strings.
    """
    all_versions = set(versions(conn, name))
    removed = sorted(all_versions - keep_versions)
    if removed:
        marks = ",".join("%s" for _ in removed)
        conn.execute(
            f"DELETE FROM fetches WHERE artifact = %(name)s AND version = ANY(%(removed)s)",
            {"name": name, "removed": removed},
        )
        conn.commit()
    return removed


def migrate_legacy(conn, legacy_json: str) -> bool:
    """Import a legacy manifest.json into the DB once (meta-flagged).

    Args:
        conn: DB connection.
        legacy_json: path to the old manifest.json (STATE_FILE).
    Returns:
        True if an import happened, False if already done or no file.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'legacy_imported'").fetchone()
    if row:
        return False
    path = Path(legacy_json)
    if not path.is_file():
        conn.execute("INSERT INTO meta VALUES ('legacy_imported', 'no-file') ON CONFLICT (key) DO NOTHING")
        conn.commit()
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    for name, rows in data.get("artifacts", {}).items():
        for e in rows:
            conn.execute(
                f"INSERT INTO fetches ({_COLS}) VALUES (%(artifact)s, %(version)s, %(filename)s, %(sha256)s, %(source)s, %(url)s, %(fetched_at)s, %(digest_verified)s)",
                {"artifact": name, "version": e.get("version", ""), "filename": e.get("filename", ""),
                 "sha256": e.get("sha256", ""), "source": e.get("source", ""), "url": e.get("url", ""),
                 "fetched_at": e.get("fetched_at", ""), "digest_verified": e.get("digest_verified", False)},
            )
    conn.execute("INSERT INTO meta VALUES ('legacy_imported', %(path)s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", {"path": str(path)})
    conn.commit()
    return True
