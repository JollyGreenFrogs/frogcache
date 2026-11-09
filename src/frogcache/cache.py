"""Cache directory operations: list cached files, download atomically, retain N newest.

The cache dir is a flat directory of files (served by nginx autoindex).
The manifest (state file) records every fetch with its sha256; retention prunes
both files and manifest entries so only the newest `retain` files per artifact
remain (default: latest + latest-1).

Governing ADR: docs/decisions/ADR-001.
"""

import hashlib
import os
import re
import shutil
import urllib.request
from pathlib import Path

from frogcache.versions import is_newer, parse_version

WHEEL_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>[A-Za-z0-9_.]+)-[^-]+-[^-]+-[^-]+\.whl$")


def parse_wheel_filename(filename: str) -> tuple[str, str] | None:
    """Split a wheel filename into (project name, version) per PEP 427.

    Args:
        filename: basename such as 'en_core_web_sm-3.8.0-py3-none-any.whl'.
    Returns:
        (name, version) with dashes normalized to underscores in name, or None
        if the filename is not a valid wheel name.
    """
    match = WHEEL_RE.match(filename)
    if not match:
        return None
    return match.group("name"), match.group("version")


def normalize_name(name: str) -> str:
    """PEP 503-normalize a project name: 'en_core_web_sm' -> 'en-core-web-sm'."""
    return re.sub(r"[-_.]+", "-", name).lower()


def cached_files(cache_dir: str, name: str) -> list[tuple[str, str]]:
    """List cached files for one artifact.

    Args:
        cache_dir: flat cache directory.
        name: normalized artifact name ('en-core-web-sm').
    Returns:
        [(version, filename)] sorted newest first.
    """
    found = []
    for path in Path(cache_dir).glob("*.whl"):
        parsed = parse_wheel_filename(path.name)
        if parsed and normalize_name(parsed[0]) == name:
            found.append((parsed[1], path.name))
    return sorted(found, key=lambda item: parse_version(item[0]), reverse=True)


def newest_cached_version(cache_dir: str, name: str) -> str | None:
    """Return the newest cached version for an artifact, or None if absent."""
    files = cached_files(cache_dir, name)
    return files[0][0] if files else None


def download_atomic(url: str, cache_dir: str, filename: str, timeout: int) -> tuple[Path, str]:
    """Download url into cache_dir/filename via a temp file + rename.

    Args:
        url: absolute download URL.
        cache_dir: flat cache directory (created if missing).
        filename: destination basename.
        timeout: request timeout in seconds.
    Returns:
        (path, sha256 hex digest) of the stored file.
    Raises:
        RuntimeError: on download failure (temp file is removed).
    """
    dest_dir = Path(cache_dir)
    dest = dest_dir / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.part"
    request = urllib.request.Request(url, headers={"User-Agent": "frogcache/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, open(tmp, "wb") as out:
            digest = hashlib.sha256()
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
        os.replace(tmp, dest)
        return dest, digest.hexdigest()
    except Exception as err:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"download {url} failed: {err}") from err


def apply_retention(cache_dir: str, name: str, keep: int) -> list[str]:
    """Delete all but the newest `keep` cached files for an artifact.

    Args:
        cache_dir: flat cache directory.
        name: normalized artifact name.
        keep: number of newest files to retain (>= 1).
    Returns:
        Filenames that were deleted.
    """
    files = cached_files(cache_dir, name)
    removed = []
    for _, filename in files[keep:]:
        (Path(cache_dir) / filename).unlink(missing_ok=True)
        removed.append(filename)
    return removed


def is_update_available(latest_version: str, cache_dir: str, name: str) -> bool:
    """Return True if latest_version is newer than the newest cached version."""
    cached = newest_cached_version(cache_dir, name)
    return cached is None or is_newer(latest_version, cached)
