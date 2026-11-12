"""Row shapes for the frogcache DB tables (dataclasses: fields + validation only).

Governing ADRs: ADR-003 §C, ADR-004.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FetchRow:
    """One row of the `fetches` table — one successfully fetched file.

    Attributes:
        artifact: artifact name.
        version: version (wheel version / HF revision sha / image digest).
        filename: path in the cache (or docker/<image>:<tag> for images).
        sha256: verified (or baseline) digest.
        source: source type label.
        url: download URL.
        fetched_at: ISO-8601 UTC timestamp.
        digest_verified: True only when sha256 matched an upstream digest.
    """

    artifact: str
    version: str
    filename: str
    sha256: str
    source: str = ""
    url: str = ""
    fetched_at: str = ""
    digest_verified: bool = False


@dataclass(frozen=True)
class EventRow:
    """One row of the `events` audit table (ADR-004).

    Attributes:
        ts: ISO-8601 UTC timestamp.
        event: event type (check_start, verify_pass, verify_fail, ...).
        artifact: artifact name (coarse identifier only).
        version: version/revision/sha.
        detail: JSON-encoded extra fields (never bodies/tokens/contents).
    """

    ts: str
    event: str
    artifact: str = ""
    version: str = ""
    detail: str = "{}"
