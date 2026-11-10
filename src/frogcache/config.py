"""Load env-only runtime configuration (house rule: runtime knobs are env-only).

Secrets (NTFY_URL, NTFY_TOKEN, GITHUB_TOKEN) may be seeded from a KEY=VALUE
file at Config.secrets_file, mounted read-only — never committed.

Governing ADR: docs/decisions/ADR-001.
"""

import os
from pathlib import Path

from frogcache.data_models.config import Config

SECRET_KEYS = ("NTFY_URL", "NTFY_TOKEN", "GITHUB_TOKEN", "REGISTRY_USER", "REGISTRY_PASS", "DATABASE_URL")


def read_secrets_file(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE secrets file.

    Args:
        path: absolute path; a missing file is not an error.
    Returns:
        Dict of KEY -> value (comments and blank lines skipped).
    """
    secrets: dict[str, str] = {}
    file = Path(path)
    if not file.is_file():
        return secrets
    for line in file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            secrets[key.strip()] = value.strip()
    return secrets


def load_config() -> Config:
    """Build a Config from environment variables, with secrets-file fallback.

    Returns:
        Validated Config.
    Raises:
        ValueError: if Config.validate() rejects the values.
    """
    secrets = read_secrets_file(os.environ.get("SECRETS_FILE", "/run/secrets/frogcache.env"))

    def secret(key: str) -> str:
        return secrets.get(key) or os.environ.get(key, "")

    config = Config(
        cache_dir=os.environ.get("CACHE_DIR", "/cache"),
        artifacts_file=os.environ.get("ARTIFACTS_FILE", "/artifacts/artifacts.json"),
        database_url=secret("DATABASE_URL"),
        registry_addr=secret("REGISTRY_ADDR"),
        registry_user=secret("REGISTRY_USER"),
        registry_pass=secret("REGISTRY_PASS"),
        legacy_state_file=os.environ.get("STATE_FILE", "/data/manifest.json"),
        secrets_file=os.environ.get("SECRETS_FILE", "/run/secrets/frogcache.env"),
        check_interval_seconds=int(os.environ.get("CHECK_INTERVAL_SECONDS", "604800")),
        retain=int(os.environ.get("RETAIN", "2")),
        http_timeout=int(os.environ.get("HTTP_TIMEOUT", "120")),
        ntfy_url=secret("NTFY_URL"),
        ntfy_token=secret("NTFY_TOKEN"),
        github_token=secret("GITHUB_TOKEN"),
        sandbox_dir=os.environ.get("SANDBOX_DIR", "/sandbox"),
        quarantine_dir=os.environ.get("QUARANTINE_DIR", "/sandbox/quarantine"),
    )
    config.validate()
    return config
