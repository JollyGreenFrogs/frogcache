"""Cache directory operations: list cached files, download atomically, retain N newest.

The cache dir is a flat directory of files (served by nginx autoindex).
The manifest (state file) records every fetch with its sha256; retention prunes
both files and manifest entries so only the newest `retain` files per artifact
remain (default: latest + latest-1).

Governing ADR: docs/decisions/ADR-001.
"""

import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import urllib.request
import uuid
from pathlib import Path

from frogcache.net import safe_url
from frogcache.versions import is_newer, parse_version

def safe_join(root: str, rel: str) -> Path:
    """Join rel onto root, rejecting absolute paths and traversal outside root.

    Args:
        root: base directory (cache dir).
        rel: relative path (may contain subdirs).
    Returns:
        Resolved path guaranteed to be inside root.
    Raises:
        RuntimeError: if the result escapes root.
    """
    base = Path(root).resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise RuntimeError(f"path escapes cache root: {rel!r}")
    return target


WHEEL_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>[A-Za-z0-9_.]+)(?:-(?P<build>\d[^-]*))?-[^-]+-[^-]+-[^-]+\.whl$")


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
    # strict filename guard: flat name only, no separators/traversal — this is
    # the sanitizer the path-injection query recognizes; safe_join re-checks
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", filename):
        raise RuntimeError(f"unsafe download filename: {filename!r}")
    dest = safe_join(cache_dir, filename)  # containment: filename may not escape
    # tmp name comes from a uuid (not from the tainted filename) so no path op
    # in the cleanup branch depends on user data
    tmp = safe_join(cache_dir, f".{uuid.uuid4().hex}.part")
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(safe_url(url), headers={"User-Agent": "frogcache/1.0"})
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


def is_update_available(latest_version: str, cache_dir: str, name: str) -> bool:
    """Return True if latest_version is newer than the newest cached version."""
    cached = newest_cached_version(cache_dir, name)
    return cached is None or is_newer(latest_version, cached)


@contextlib.contextmanager
def retention_lock(files_dir: str):
    """Cross-process lock on the files share so updater/fetcher retention never races."""
    path = Path(files_dir).resolve() / ".retention.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def apply_retention_versions(cache_dir: str, name: str, keep_versions: set[str]) -> list[str]:
    """Delete cached files of `name` whose version is not in keep_versions (DB-aligned)."""
    removed = []
    for version, filename in cached_files(cache_dir, name):
        if version not in keep_versions:
            (Path(cache_dir) / filename).unlink(missing_ok=True)
            removed.append(filename)
    return removed
