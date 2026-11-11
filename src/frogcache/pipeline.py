"""Per-artifact update pipeline (ADR-003 §D staged flow): resolve -> sandbox
download -> verify -> promote -> manifest -> retention, with audit logging.

Governing ADRs: ADR-001/002/003/004.
"""

import re
import urllib.parse
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
        root=config.cache_dir,
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
    if release.version in cached and hf.revision_complete(
        config.cache_dir, spec.source.repo, release.version,
        [path for path, _, _ in release.files], entries,
    ):
        return f"{spec.name}: up to date ({release.version[:12]})"
    # promote + DB record + prune under ONE lock: a concurrent pull-through
    # prune can never delete the in-flight revision's files before they are
    # recorded (which would later report success with missing files)
    with cache.retention_lock(config.cache_dir):
        downloaded = []
        for path, url, expected_sha256 in release.files:
            dest = cache.safe_join(
                str(hf.hf_dir(config.cache_dir, spec.source.repo, release.version)), path
            )
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
        removed = hf.apply_hf_retention(config.cache_dir, spec.source.repo, kept | {release.version})
    # never report success with missing files: every promoted file must exist post-retention
    missing = [p for p, _, _ in release.files
               if not (hf.hf_dir(config.cache_dir, spec.source.repo, release.version) / p).is_file()]
    if missing:
        raise RuntimeError(f"post-retention check: missing files {missing}")
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
    dest = cache.safe_join(config.cache_dir, release.filename)
    _log(conn, "download_start", spec.name, release.version, file=release.filename)
    # promote + DB record + disk prune under ONE lock: a concurrent pull-through
    # prune can never delete the in-flight/just-promoted file before it is
    # recorded, and never observe a keep-set that deletes a retained version
    with cache.retention_lock(config.cache_dir):
        sha256, verified = _stage_and_promote(config, release.url, dest, release.sha256, _digest_required(spec.source.type, release.filename))
        _log(conn, "verify_pass", spec.name, release.version, file=release.filename, digest_verified=verified)
        manifest.record_fetch(
            conn, spec.name,
            ManifestEntry(version=release.version, filename=dest.name, sha256=sha256,
                          source=spec.source.type, url=release.url,
                          fetched_at=datetime.now(timezone.utc).isoformat(),
                          digest_verified=verified),
            config.retain,
        )
        # prune DISK to the same fetch-time keep-set the DB was just pruned to —
        # version-ordered pruning here could delete a manifest-retained wheel or
        # keep an unrecorded one
        kept = set(manifest.versions(conn, spec.name))
        removed = cache.apply_retention_versions(config.cache_dir, spec.name, kept)
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
    dest = cache.safe_join(config.cache_dir, path)  # sanitizer: rejects traversal
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
        # hf/<org>/<model>/<revision>/<file...>
        if len(parts) < 5:
            return None
        repo, rev, rel = f"{parts[1]}/{parts[2]}", parts[3], "/".join(parts[4:])
        if any(seg in ("", ".", "..") for seg in parts[1:]):
            return None  # traversal or empty segments (repo/revision/file) never served
        # resolve a mutable revision (branch/tag like 'main') to its commit sha
        # first: the cache key and the downloaded bytes must be the same commit
        sha = rev
        if not re.fullmatch(r"[0-9a-f]{40}", rev):
            try:
                info = hf.fetch_json(
                    hf.HF_API_MODEL_URL.format(repo=repo) + "/revision/"
                    + urllib.parse.quote(rev, safe=""),
                    timeout=config.http_timeout,
                )
            except RuntimeError:
                return None
            sha = info.get("sha", "")
            if not sha:
                return None
        dest = cache.safe_join(config.cache_dir, f"hf/{repo}/{sha}/{rel}")
        url = (
            f"https://huggingface.co/{repo}/resolve/{urllib.parse.quote(sha, safe='')}"
            f"/{urllib.parse.quote(rel, safe='/')}"
        )
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
    """Record a successful pull-through fetch, then prune DISK to match the DB (ADR-003 §A).

    Caller must hold cache.retention_lock across the promote + this call, so the
    updater can never race us and delete the just-fetched version before it is
    recorded.
    """
    manifest.record_fetch(
        conn, spec.name,
        ManifestEntry(version=version, filename=filename, sha256=sha256,
                      source=spec.source.type, url="",
                      fetched_at=datetime.now(timezone.utc).isoformat(),
                      digest_verified=verified),
        config.retain,
    )
    _apply_disk_retention(config, conn, spec, keep_extra={version})


def _apply_disk_retention(config: Config, conn, spec, keep_extra: set[str] | None = None) -> None:
    """Prune cached files to the newest RETAIN versions recorded in the DB.

    Wheels: top-level *.whl per artifact. HF: revision dirs per repo, with the
    keep-set shared across all artifacts tracking that repo. Never deletes the
    file currently being served (it is in the keep-set by construction).
    Caller must hold cache.retention_lock (DB prune + disk prune are atomic).
    """
    if spec.source.type == "pypi":
        # DB-aligned keep-set (fetch-time): deletes unrecorded files and never
        # deletes a version the manifest retained; keep_extra protects the
        # just-fetched pull-through version
        kept = set(manifest.versions(conn, spec.name)) | (keep_extra or set())
        cache.apply_retention_versions(config.cache_dir, spec.name, kept)
        return
    if spec.source.type == "huggingface":
        try:
            all_specs = upstream.load_artifacts(config.artifacts_file)
        except ValueError:
            all_specs = []
        if spec not in all_specs:
            all_specs.append(spec)  # pull-through spec may not be persisted yet
        seen: dict[str, str] = {}
        for other in all_specs:
            if other.source.type == "huggingface" and other.source.repo == spec.source.repo:
                for e in manifest.entries(conn, other.name):
                    seen[e["version"]] = max(seen.get(e["version"], ""), e["fetched_at"])
        kept = set(sorted(seen, key=lambda v: seen[v], reverse=True)[: config.retain]) | (keep_extra or set())
        hf.apply_hf_retention(config.cache_dir, spec.source.repo, kept)
