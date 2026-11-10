"""Per-artifact update pipeline (ADR-003 §D staged flow): resolve -> sandbox
download -> verify -> promote -> manifest -> retention, with audit logging.

Governing ADRs: ADR-001/002/003/004.
"""

from datetime import datetime, timezone
from pathlib import Path

from frogcache import audit, cache, docker_source, hf, manifest, net, upstream, verify
from frogcache.data_models.artifact import ManifestEntry
from frogcache.data_models.config import Config

# Source types whose upstreams expose a sha256 digest for payload files.
# Metadata files (json/md/txt) legitimately have none — those pass unverified.
DIGEST_REQUIRED_SOURCES = ("pypi", "huggingface", "github-releases")
DIGEST_REQUIRED_SUFFIXES = (".whl", ".gguf", ".safetensors", ".bin", ".pt", ".tar.gz")


def _digest_required(source_type: str, filename: str) -> bool:
    """True when a payload file from a digest-capable source must carry a sha256."""
    return source_type in DIGEST_REQUIRED_SOURCES and filename.lower().endswith(
        DIGEST_REQUIRED_SUFFIXES
    )


def _log(conn, event: str, artifact: str = "", version: str = "", **detail) -> None:
    """Write one audit event; no-op when no audit connection."""
    if conn is not None:
        audit.log_event(conn, event, artifact, version, **detail)


def _stage_and_promote(config: Config, url: str, dest: Path, expected_sha256: str,
                       digest_required: bool) -> tuple[str, bool]:
    """Download to sandbox, verify, promote to dest. Returns (sha256, digest_verified).

    Raises:
        RuntimeError: download or verification failed (file quarantined, not served).
    """
    ok, sha256, verified = verify.fetch_and_promote(
        url,
        dest,
        Path(config.sandbox_dir),
        config.http_timeout,
        expected_sha256=expected_sha256,
        quarantine=Path(config.quarantine_dir),
        digest_required=digest_required,
    )
    if not ok:
        raise RuntimeError(f"verification failed for {dest.name}; quarantined, not served")
    return sha256, verified


def check_hf_artifact(config: Config, spec, conn, all_specs=()) -> str:
    """Check one HuggingFace model: staged download of all matching files if the revision is new.

    Args:
        config: runtime config.
        spec: ArtifactSpec with source.type == 'huggingface'.
        conn: audit DB connection (or None).
    Returns:
        Report line ('updated ...', 'up to date ...', 'no files found upstream').
    Raises:
        RuntimeError: on upstream or verification failure.
    """
    release = hf.latest_hf_release(spec, config.http_timeout)
    if release is None:
        return f"{spec.name}: no files found upstream"
    cached = hf.cached_hf_versions(config.cache_dir, spec.source.repo)
    entries = manifest.entries(conn, spec.name)
    # Skip only if the newest cached revision is complete (files on disk AND in
    # the manifest) — a failed download or a changed files pattern must retry.
    if cached and cached[0] == release.version and hf.revision_complete(
        config.cache_dir, spec.source.repo, release.version,
        [path for path, _, _ in release.files], entries,
    ):
        return f"{spec.name}: up to date ({release.version[:12]})"
    downloaded = []
    for path, url, expected_sha256 in release.files:
        dest = hf.hf_dir(config.cache_dir, spec.source.repo, release.version) / path
        _log(conn, "download_start", spec.name, release.version, file=path)
        sha256, verified = _stage_and_promote(
            config, url, dest, expected_sha256, _digest_required(spec.source.type, path)
        )
        _log(conn, "verify_pass", spec.name, release.version, file=path, digest_verified=verified)
        downloaded.append((f"hf/{spec.source.repo}/{release.version}/{path}", sha256, verified))
    manifest.record_fetch_multi(conn, spec.name, release.version, downloaded,
                                spec.source.type, datetime.now(timezone.utc).isoformat())
    # keep-set spans ALL artifacts tracking this repo — per-artifact pruning
    # would delete another spec's revisions (e.g. main vs pinned tag)
    seen: dict[str, str] = {}  # version -> newest fetched_at across the repo
    for other in all_specs or [spec]:
        if other.source.type == "huggingface" and other.source.repo == spec.source.repo:
            for e in manifest.entries(conn, other.name):
                seen[e["version"]] = max(seen.get(e["version"], ""), e["fetched_at"])
    kept = set(sorted(seen, key=lambda v: seen[v], reverse=True)[: config.retain])
    for other in all_specs or [spec]:
        if other.source.type == "huggingface" and other.source.repo == spec.source.repo:
            manifest.prune_versions(conn, other.name, kept)
    removed = hf.apply_hf_retention(config.cache_dir, spec.source.repo, kept)
    _log(conn, "retention", spec.name, release.version, removed=removed)
    before = cached[0][:12] if cached else "none"
    note = f" (pruned {', '.join(v[:12] for v in removed)})" if removed else ""
    return f"{spec.name}: updated {before} -> {release.version[:12]} ({len(downloaded)} files){note}"


def check_artifact(config: Config, spec, conn, all_specs=()) -> str:
    """Check one single-file artifact (pypi / github-*): staged download + retain if newer.

    Args:
        config: runtime config.
        spec: ArtifactSpec to check.
        conn: audit DB connection (or None).
    Returns:
        Report line describing the outcome.
    Raises:
        RuntimeError: if the upstream check, download, or verification fails.
    """
    if spec.source.type == "huggingface":
        return check_hf_artifact(config, spec, conn, all_specs)
    if spec.source.type == "docker":
        return docker_source.check_docker_artifact(config, spec, conn)
    release = upstream.latest_release(spec, config.github_token, config.http_timeout)
    if release is None:
        return f"{spec.name}: no upstream release found"
    if not cache.is_update_available(release.version, config.cache_dir, spec.name):
        return f"{spec.name}: up to date ({release.version})"
    cached_before = cache.newest_cached_version(config.cache_dir, spec.name) or "none"
    dest = Path(config.cache_dir) / release.filename
    _log(conn, "download_start", spec.name, release.version, file=release.filename)
    sha256, verified = _stage_and_promote(config, release.url, dest, release.sha256, _digest_required(spec.source.type, release.filename))
    _log(conn, "verify_pass", spec.name, release.version, file=release.filename, digest_verified=verified)
    removed = cache.apply_retention(config.cache_dir, spec.name, config.retain)
    manifest.record_fetch(
        conn, spec.name,
        ManifestEntry(version=release.version, filename=dest.name, sha256=sha256,
                      source=spec.source.type, url=release.url,
                      fetched_at=datetime.now(timezone.utc).isoformat(),
                      digest_verified=verified),
        config.retain,
    )
    _log(conn, "retention", spec.name, release.version, removed=removed)
    note = f" (pruned {', '.join(removed)})" if removed else ""
    return f"{spec.name}: updated {cached_before} -> {release.version}{note}"




def fetch_exact_file(config: Config, path: str, spec, conn) -> Path | None:
    """Fetch the EXACT requested file (not the latest release) via the staged flow.

    Wheel paths resolve name+version via the PyPI JSON API; hf/<org>/<model>/<sha>/<file>
    resolves the pinned sha directly. Returns the promoted cache path, or None.

    Args:
        config: runtime config.
        path: requested URL path.
        spec: inferred ArtifactSpec (weekly tracking spec).
        conn: DB connection for audit events.
    Returns:
        Promoted Path, or None if the exact file cannot be resolved.
    Raises:
        RuntimeError: download/verification failed.
    """
    dest = Path(config.cache_dir) / path
    if spec.source.type == "pypi":
        parsed = cache.parse_wheel_filename(path)
        if not parsed:
            return None
        name, version = parsed
        data = net.fetch_json(
            f"https://pypi.org/pypi/{cache.normalize_name(name)}/{version}/json",
            timeout=config.http_timeout,
        )
        match = next((f for f in data.get("urls", []) if f.get("filename") == path), None)
        if match is None:
            return None
        sha256, verified = _stage_and_promote(
            config, match["url"], dest,
            match.get("digests", {}).get("sha256", ""), _digest_required("pypi", path),
        )
        _record_pullthrough(conn, config, spec, version, path, sha256, verified)
        audit.log_event(conn, "promote", spec.name, version, file=path, digest_verified=verified)
        return dest
    if spec.source.type == "huggingface":
        parts = path.split("/")
        # hf/<org>/<model>/<sha>/<file...>
        if len(parts) < 5:
            return None
        repo, sha, rel = f"{parts[1]}/{parts[2]}", parts[3], "/".join(parts[4:])
        url = f"https://huggingface.co/{repo}/resolve/{sha}/{rel}"
        # upstream integrity: the tree entry's LFS oid for this exact file
        expected = ""
        try:
            tree = hf.fetch_tree_pages(
                hf.HF_API_TREE_URL.format(repo=repo, revision=sha), timeout=config.http_timeout
            )
            entry = next((e for e in tree if e.get("path") == rel), None)
            expected = (entry or {}).get("lfs", {}).get("oid", "").removeprefix("sha256:")
        except RuntimeError:
            pass  # no digest available -> baseline recorded, digest_verified=False
        sha256, verified = _stage_and_promote(
            config, url, dest, expected, _digest_required("huggingface", rel)
        )
        _record_pullthrough(conn, config, spec, sha, f"hf/{repo}/{sha}/{rel}", sha256, verified)
        audit.log_event(conn, "promote", spec.name, sha, file=rel, digest_verified=verified)
        return dest
    return None


def _record_pullthrough(conn, config: Config, spec, version: str, filename: str,
                        sha256: str, verified: bool) -> None:
    """Record a successful pull-through fetch in the fetches table (ADR-003 §A)."""
    manifest.record_fetch(
        conn, spec.name,
        ManifestEntry(version=version, filename=filename, sha256=sha256,
                      source=spec.source.type, url="",
                      fetched_at=datetime.now(timezone.utc).isoformat(),
                      digest_verified=verified),
        config.retain,
    )
