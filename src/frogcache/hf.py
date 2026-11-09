"""HuggingFace model hosting: resolve a model repo's files at a revision and cache them.

Cache layout: hf/<org>/<model>/<revision-sha>/<path...>, served by the nginx
autoindex at http://<cache-host>:8091/hf/<org>/<model>/<sha>/<path>. The
revision sha acts as the artifact "version"; retention keeps the newest
RETAIN revision directories per model (rollback = previous sha's files).

Consumers fetch files over plain HTTP — no huggingface.co needed at consume
time. Governing ADR: docs/decisions/ADR-002-hf-model-hosting.md.
"""

import fnmatch
import shutil
from pathlib import Path

from frogcache.data_models.artifact import ArtifactSpec, UpstreamRelease
from frogcache.upstream import fetch_json

HF_API_MODEL_URL = "https://huggingface.co/api/models/{repo}"
HF_API_TREE_URL = "https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=true&limit=1000"
HF_RESOLVE_URL = "https://huggingface.co/{repo}/resolve/{revision}/{path}"


def latest_hf_release(spec: ArtifactSpec, timeout: int = 60) -> UpstreamRelease | None:
    """Resolve the file list of a HF model repo at a revision.

    Args:
        spec: artifact spec with source.type == 'huggingface'; source.repo is
            'org/model', source.revision the branch/tag/commit (default 'main'),
            source.files optional fnmatch patterns (empty = all files).
        timeout: per-request timeout in seconds.
    Returns:
        UpstreamRelease with version = revision commit sha and files =
        ((path, resolve_url), ...) (url/filename empty — multi-file mode), or
        None if the repo/revision has no matching files.
    Raises:
        RuntimeError: if the HF API cannot be reached.
    Note:
        the tree API paginates (~1000 entries per request); repos with more
        files need HF_API_TREE_URL's limit raised.
    """
    source = spec.source
    revision_info = fetch_json(
        HF_API_MODEL_URL.format(repo=source.repo) + f"/revision/{source.revision}",
        timeout=timeout,
    )
    sha = revision_info.get("sha", "")
    if not sha:
        return None
    # Query the tree and build resolve URLs with the resolved sha, not the
    # mutable revision name: 'main' can advance between requests (or during a
    # multi-GB download), which would store mixed-commit files under one sha.
    tree = fetch_json(
        HF_API_TREE_URL.format(repo=source.repo, revision=sha), timeout=timeout
    )
    files = []
    for entry in tree:
        if entry.get("type") != "file":
            continue
        path = entry.get("path", "")
        if source.files and not any(fnmatch.fnmatch(path, pat) for pat in source.files):
            continue
        files.append(
            (path, HF_RESOLVE_URL.format(repo=source.repo, revision=sha, path=path))
        )
    if not files:
        return None
    return UpstreamRelease(version=sha, url="", filename="", files=tuple(files))


def hf_dir(cache_dir: str, repo: str, version: str) -> Path:
    """Directory holding one revision of a HF model: <cache_dir>/hf/<repo>/<version>."""
    return Path(cache_dir) / "hf" / repo / version


def cached_hf_versions(cache_dir: str, repo: str) -> list[str]:
    """List cached HF revisions for a repo, newest first (by directory mtime).

    Args:
        cache_dir: flat cache directory.
        repo: 'org/model'.
    Returns:
        Revision shas, newest first; empty if none cached.
    """
    base = Path(cache_dir) / "hf" / repo
    if not base.is_dir():
        return []
    return sorted(
        (p.name for p in base.iterdir() if p.is_dir()),
        key=lambda name: (base / name).stat().st_mtime,
        reverse=True,
    )


def apply_hf_retention(cache_dir: str, repo: str, keep: int) -> list[str]:
    """Delete all but the newest `keep` revision dirs of a HF model.

    Args:
        cache_dir: flat cache directory.
        repo: 'org/model'.
        keep: number of newest revisions to retain (>= 1).
    Returns:
        Revision shas that were removed.
    """
    removed = []
    for version in cached_hf_versions(cache_dir, repo)[keep:]:
        shutil.rmtree(hf_dir(cache_dir, repo, version), ignore_errors=True)
        removed.append(version)
    return removed


def revision_complete(
    cache_dir: str,
    repo: str,
    version: str,
    expected_paths: list[str],
    manifest_entries: list[dict],
) -> bool:
    """True if a cached revision has every expected file on disk AND in the manifest.

    Guards the 'up to date' skip: a failed download must leave the revision
    retryable, and a changed `source.files` pattern (same sha) must not skip
    newly selected files.

    Args:
        cache_dir: flat cache directory.
        repo: 'org/model'.
        version: revision sha.
        expected_paths: file paths (relative to the revision dir) the release lists.
        manifest_entries: manifest entries for this artifact (any version).
    Returns:
        True only if all expected files exist and are recorded for this version.
    """
    dest = hf_dir(cache_dir, repo, version)
    recorded = {
        e["filename"] for e in manifest_entries if e.get("version") == version
    }
    for path in expected_paths:
        if not (dest / path).is_file():
            return False
        if f"hf/{repo}/{version}/{path}" not in recorded:
            return False
    return True
