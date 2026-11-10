"""Runtime configuration model (fields + validation only; loaded by config.py).

Governing ADR: docs/decisions/ADR-001.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """Env-only runtime configuration for the updater.

    Attributes:
        cache_dir: directory holding the cached files (served by nginx).
        artifacts_file: JSON file with the artifact specs.
        database_url: Postgres DSN (env DATABASE_URL via secrets file) — the store.
        legacy_state_file: old manifest.json path, imported once into the DB.
        secrets_file: KEY=VALUE file with NTFY_URL/NTFY_TOKEN/GITHUB_TOKEN.
        check_interval_seconds: loop sleep; default weekly (604800).
        retain: how many newest files to keep per artifact (latest + latest-1 => 2).
        http_timeout: per-request timeout in seconds (downloads can be large).
        ntfy_url: ntfy topic URL for notifications (empty disables notify).
        ntfy_token: bearer token for the ntfy topic.
        github_token: optional token for api.github.com.
        sandbox_dir: air-gapped staging dir for downloads before verification
            (ADR-003 §D); mounted only into the updater.
        quarantine_dir: quarantine for failed verification, inside the sandbox.
    """

    cache_dir: str
    artifacts_file: str
    secrets_file: str
    database_url: str = ""
    legacy_state_file: str = "/data/manifest.json"
    check_interval_seconds: int = 604800
    retain: int = 2
    http_timeout: int = 120
    ntfy_url: str = ""
    ntfy_token: str = ""
    github_token: str = ""
    registry_addr: str = ""
    registry_user: str = ""
    registry_pass: str = ""
    sandbox_dir: str = "/sandbox"
    quarantine_dir: str = "/sandbox/quarantine"

    def validate(self) -> None:
        """Raise ValueError on nonsensical values."""
        if self.retain < 1:
            raise ValueError("retain must be >= 1")
        if self.check_interval_seconds < 60:
            raise ValueError("check_interval_seconds must be >= 60")
