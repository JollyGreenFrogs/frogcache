"""Manifest (state file) operations: load/save atomically, record fetches, prune versions.

The manifest records every successful fetch with its sha256; retention prunes
entries so only the newest `retain` versions per artifact remain. Split from
cache.py (file operations) per the one-concern-per-module house rule.

Governing ADR: docs/decisions/ADR-001 (wheels) and ADR-002 (HF revisions).
"""

import json
from pathlib import Path

from frogcache.data_models.artifact import ManifestEntry
from frogcache.versions import parse_version


def load_manifest(state_file: str) -> dict:
    """Load the manifest JSON; missing/corrupt file -> empty manifest."""
    try:
        manifest = json.loads(Path(state_file).read_text())
        manifest.setdefault("artifacts", {})
        return manifest
    except (OSError, json.JSONDecodeError):
        return {"artifacts": {}}


def save_manifest(state_file: str, manifest: dict) -> None:
    """Persist the manifest atomically (temp file + rename)."""
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(path)


def record_fetch(manifest: dict, name: str, entry: ManifestEntry, keep: int) -> None:
    """Append a fetch entry to the manifest and prune entries beyond `keep`.

    Args:
        manifest: manifest dict (mutated).
        name: normalized artifact name.
        entry: ManifestEntry for the fetch.
        keep: number of newest entries to retain for the artifact.
    """
    entries = manifest["artifacts"].setdefault(name, [])
    entries.append(entry.__dict__)
    entries.sort(key=lambda e: parse_version(e["version"]), reverse=True)
    del entries[keep:]


def record_fetch_multi(
    manifest: dict,
    name: str,
    version: str,
    files: list[tuple[str, str]],
    source: str,
    fetched_at: str,
) -> None:
    """Append one manifest entry per downloaded file (multi-file fetch).

    Used for HuggingFace model fetches (ADR-002): one fetch = many files sharing
    one version (revision sha). Only successful fetches are recorded, so
    manifest versions == successful revisions; pruning happens separately via
    prune_manifest_versions so disk and manifest retention can use the same
    keep-set.

    Args:
        manifest: manifest dict (mutated).
        name: artifact name.
        version: version (revision sha) all files belong to.
        files: [(filename, sha256)] pairs to record.
        source: source type label.
        fetched_at: ISO-8601 UTC timestamp for the new entries.
    """
    entries = manifest["artifacts"].setdefault(name, [])
    for filename, sha256 in files:
        entries.append(
            {
                "version": version,
                "filename": filename,
                "sha256": sha256,
                "source": source,
                "url": "",
                "fetched_at": fetched_at,
            }
        )


def manifest_versions(manifest: dict, name: str) -> list[str]:
    """Versions recorded in the manifest for an artifact, newest first (by fetched_at).

    Args:
        manifest: manifest dict.
        name: artifact name.
    Returns:
        Version strings, newest first; empty if the artifact has no entries.
    """
    entries = manifest["artifacts"].get(name, [])
    return sorted(
        {e["version"] for e in entries},
        key=lambda v: max(e["fetched_at"] for e in entries if e["version"] == v),
        reverse=True,
    )


def prune_manifest_versions(manifest: dict, name: str, keep_versions: set[str]) -> list[str]:
    """Drop manifest entries whose version is not in keep_versions.

    Args:
        manifest: manifest dict (mutated).
        name: artifact name.
        keep_versions: versions to keep.
    Returns:
        Removed version strings.
    """
    entries = manifest["artifacts"].get(name, [])
    removed = sorted({e["version"] for e in entries} - keep_versions)
    manifest["artifacts"][name] = [e for e in entries if e["version"] in keep_versions]
    return removed
