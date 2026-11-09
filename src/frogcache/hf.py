"""HuggingFace model hosting: resolve a model repo's files at a revision and cache them.

Cache layout: hf/<org>/<model>/<revision-sha>/<path...>, served by the nginx
autoindex at http://<cache-host>:8091/hf/<org>/<model>/<sha>/<path>. The
revision sha acts as the artifact "version"; retention keeps the newest
RETAIN revision directories per model (rollback = previous sha's files).

Consumers fetch files over plain HTTP — no huggingface.co needed at consume
time. Governing ADR: docs/decisions/ADR-002-hf-model-hosting.md.
"""

import fnmatch
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from frogcache.data_models.artifact import ArtifactSpec, UpstreamRelease
from frogcache.upstream import fetch_json

HF_API_MODEL_URL = "https://huggingface.co/api/models/{repo}"
HF_API_TREE_URL = "https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=true"
HF_RESOLVE_URL = "https://huggingface.co/{repo}/resolve/{revision}/{path}"


def fetch_tree_pages(url: str, timeout: int = 60) -> list:
    """GET the HF tree endpoint and follow Link rel="next" pagination to the end.

    fetch_json discards response headers, so pagination is handled here: every
    page is collected before the file list is built — otherwise matching files
    on later pages would be silently omitted and the partial selection treated
    as a complete revision.

    Args:
        url: absolute tree URL returning a JSON array, paginated via Link headers.
        timeout: per-request timeout in seconds.
    Returns:
        All entries from all pages.
    Raises:
        RuntimeError: on HTTP/network error.
    """
    items: list = []
    next_url: str = url
    while next_url:
        request = urllib.request.Request(
            next_url, headers={"User-Agent": "frogcache/1.0", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                items.extend(json.loads(response.read().decode()))
                link = response.headers.get("Link", "")
        except urllib.error.HTTPError as err:
            raise RuntimeError(f"GET {next_url} -> HTTP {err.code}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"GET {next_url} failed: {err.reason}") from err
        next_url = ""
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
    return items


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
        None if the repo/revision has no matching files. All tree pages are
        followed (Link rel="next") before the file list is built.
    Raises:
        RuntimeError: if the HF API cannot be reached.
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
    tree = fetch_tree_pages(
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


def apply_hf_retention(cache_dir: str, repo: str, keep_versions: set[str]) -> list[str]:
    """Remove every cached revision dir of a HF model that is not in keep_versions.

    Incomplete revisions (dirs without manifest entries, e.g. left by a failed
    download) are removed too, so disk retention and manifest pruning agree:
    both keep exactly the newest RETAIN successful revisions.

    Args:
        cache_dir: flat cache directory.
        repo: 'org/model'.
        keep_versions: revision shas to keep.
    Returns:
        Revision shas that were removed.
    """
    removed = []
    base = Path(cache_dir) / "hf" / repo
    if not base.is_dir():
        return removed
    for path in base.iterdir():
        if path.is_dir() and path.name not in keep_versions:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path.name)
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
