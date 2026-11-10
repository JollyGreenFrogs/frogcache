"""Tests for frogcache.audit (ADR-004 SQLite audit log)."""

from frogcache import audit


def test_connect_log_recent(db_conn):
    conn = db_conn
    audit.log_event(conn, "check_start", "en-core-web-sm")
    audit.log_event(conn, "verify_fail", "qwen3-8b", "abc123", digest_verified=False, reason="mismatch")
    events = audit.recent_events(conn, limit=10)
    assert [e["event"] for e in events] == ["verify_fail", "check_start"]
    assert events[0]["artifact"] == "qwen3-8b"
    assert '"digest_verified": false' in events[0]["detail"]
