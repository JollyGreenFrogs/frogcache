"""CLI entry point: run update checks once (--once) or on a loop (--loop).

Per check: for each artifact in the artifacts file, resolve the latest
upstream release; if newer than the newest cached file, download it into the
cache, record it in the manifest (sha256), apply retention (keep newest
RETAIN files per artifact), and send an ntfy info notification. Failures are
reported per artifact and summarized in one ntfy alert.

Governing ADR: docs/decisions/ADR-001.
"""

import argparse
import sys
import time
from datetime import datetime, timezone

from frogcache import cache, hf, upstream
from frogcache.config import load_config
from frogcache.data_models.artifact import ManifestEntry
from frogcache.data_models.config import Config
from frogcache.notify import notify_safe


def check_hf_artifact(config: Config, spec) -> str:
    """Check one HuggingFace model: download all matching files if the revision is new.

    Files land under hf/<repo>/<revision-sha>/ (ADR-002); retention keeps the
    newest RETAIN revisions. Returns a report line; raises RuntimeError on
    upstream/download failure.

    Args:
        config: runtime config.
        spec: ArtifactSpec with source.type == 'huggingface'.
    Returns:
        Report line ('updated ...', 'up to date ...', 'no files found upstream').
    Raises:
        RuntimeError: if the upstream check or a download fails.
    """
    release = hf.latest_hf_release(spec, config.http_timeout)
    if release is None:
        return f"{spec.name}: no files found upstream"
    cached = hf.cached_hf_versions(config.cache_dir, spec.source.repo)
    if cached and cached[0] == release.version:
        return f"{spec.name}: up to date ({release.version[:12]})"
    dest = cache.hf_dir(config.cache_dir, spec.source.repo, release.version)
    downloaded = []
    for path, url in release.files:
        _, sha256 = cache.download_atomic(url, str(dest), path, config.http_timeout)
        downloaded.append((f"hf/{spec.source.repo}/{release.version}/{path}", sha256))
    removed = cache.apply_hf_retention(config.cache_dir, spec.source.repo, config.retain)
    manifest = cache.load_manifest(config.state_file)
    cache.record_fetch_multi(
        manifest,
        spec.name,
        release.version,
        downloaded,
        spec.source.type,
        config.retain,
        datetime.now(timezone.utc).isoformat(),
    )
    cache.save_manifest(config.state_file, manifest)
    before = cached[0][:12] if cached else "none"
    note = f" (pruned {', '.join(v[:12] for v in removed)})" if removed else ""
    return f"{spec.name}: updated {before} -> {release.version[:12]} ({len(downloaded)} files){note}"


def check_artifact(config: Config, spec) -> str:
    """Check one artifact; download + retain if a newer upstream version exists.

    Args:
        config: runtime config.
        spec: ArtifactSpec to check.
    Returns:
        Report line describing the outcome ('updated ...', 'up to date ...').
    Raises:
        RuntimeError: if the upstream check or download fails.
    """
    if spec.source.type == "huggingface":
        return check_hf_artifact(config, spec)
    release = upstream.latest_release(spec, config.github_token, config.http_timeout)
    if release is None:
        return f"{spec.name}: no upstream release found"
    if not cache.is_update_available(release.version, config.cache_dir, spec.name):
        return f"{spec.name}: up to date ({release.version})"
    cached_before = cache.newest_cached_version(config.cache_dir, spec.name) or "none"
    path, sha256 = cache.download_atomic(
        release.url, config.cache_dir, release.filename, config.http_timeout
    )
    removed = cache.apply_retention(config.cache_dir, spec.name, config.retain)
    manifest = cache.load_manifest(config.state_file)
    cache.record_fetch(
        manifest,
        spec.name,
        ManifestEntry(
            version=release.version,
            filename=path.name,
            sha256=sha256,
            source=spec.source.type,
            url=release.url,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        ),
        config.retain,
    )
    cache.save_manifest(config.state_file, manifest)
    note = f" (pruned {', '.join(removed)})" if removed else ""
    return f"{spec.name}: updated {cached_before} -> {release.version}{note}"


def run_once(config: Config) -> tuple[list[str], list[str]]:
    """Run one update pass over all configured artifacts.

    Args:
        config: runtime config.
    Returns:
        (reports, failures): report lines for succeeded artifacts, error lines
        for failed ones. Failures also trigger one high-priority ntfy alert.
    """
    specs = upstream.load_artifacts(config.artifacts_file)
    reports, failures = [], []
    for spec in specs:
        try:
            reports.append(check_artifact(config, spec))
        except (RuntimeError, ValueError) as err:
            failures.append(f"{spec.name}: {err}")
            print(f"ERROR {spec.name}: {err}", file=sys.stderr)
    if failures:
        notify_safe(
            config, "frogcache: update check FAILED", "\n".join(failures), priority="high"
        )
    updated = [line for line in reports if ": updated " in line]
    if updated:
        notify_safe(config, f"frogcache: {len(updated)} artifact(s) updated", "\n".join(updated))
    return reports, failures


def main() -> int:
    """Entry point: --once runs a single pass, --loop (default) repeats weekly.

    Returns:
        Exit code: 0 after a successful --once pass, 1 if any artifact failed.
    """
    parser = argparse.ArgumentParser(prog="frogcache", description=__doc__)
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    parser.add_argument("--loop", action="store_true", help="run forever (default)")
    args = parser.parse_args()
    config = load_config()
    if not config.ntfy_url or not config.ntfy_token:
        print("warning: NTFY_URL/NTFY_TOKEN not set; updates run but notifications are off",
              file=sys.stderr)
    while True:
        reports, failures = run_once(config)
        for line in reports:
            print(line)
        if args.once:
            return 1 if failures else 0
        time.sleep(config.check_interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
