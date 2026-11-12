"""CLI entry point: --once / --loop (weekly) update checks, --fetch-server (pull-through), --audit.

Governing ADRs: ADR-001 (wheels), ADR-002 (HF), ADR-003 (pull-through/docker/
storage/verify), ADR-004 (audit log).
"""

import argparse
import sys
import time

from frogcache import audit, db, fetch_server, manifest, pipeline, upstream
from frogcache.config import load_config
from frogcache.notify import notify_safe


def _refresh_av_definitions() -> None:
    """Best-effort ClamAV signature refresh before each update pass; never raises."""
    import subprocess

    try:
        subprocess.run(["freshclam", "--quiet"], capture_output=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass  # signatures from image build time are used instead


def run_once(config) -> tuple[list[str], list[str]]:
    """Run one update pass over all configured artifacts.

    Returns:
        (reports, failures): report lines for succeeded artifacts, error lines
        for failed ones. Failures also trigger one high-priority ntfy alert.
    """
    conn = db.connect(config.database_url)
    manifest.migrate_legacy(conn, config.legacy_state_file)
    specs = upstream.load_artifacts(config.artifacts_file)
    _refresh_av_definitions()  # signatures can go stale during a long --loop run
    reports, failures = [], []
    for spec in specs:
        audit.log_event(conn, "check_start", spec.name)
        try:
            reports.append(pipeline.check_artifact(config, spec, conn, all_specs=specs))
        except (RuntimeError, ValueError, OSError) as err:
            audit.log_event(conn, "error", spec.name, error=str(err)[:200])
            failures.append(f"{spec.name}: {err}")
            print(f"ERROR {spec.name}: {err}", file=sys.stderr)
    if failures:
        notify_safe(config, "frogcache: update check FAILED", "\n".join(failures), priority="high")
    updated = [line for line in reports if ": updated " in line]
    if updated:
        notify_safe(config, f"frogcache: {len(updated)} artifact(s) updated", "\n".join(updated))
    return reports, failures


def main() -> int:
    """Entry point: --once single pass, --loop (default) weekly, --fetch-server pull-through, --audit tail.

    Returns:
        Exit code: 0 after a successful --once pass, 1 if any artifact failed.
    """
    parser = argparse.ArgumentParser(prog="frogcache", description=__doc__)
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    parser.add_argument("--loop", action="store_true", help="run forever (default)")
    parser.add_argument("--fetch-server", action="store_true", help="run the pull-through fetch server (ADR-003 §A)")
    parser.add_argument("--audit", action="store_true", help="print recent audit events and exit")
    args = parser.parse_args()
    config = load_config()
    if args.audit:
        conn = db.connect(config.database_url)
        for event in audit.recent_events(conn, limit=50):
            print(event)
        return 0
    if args.fetch_server:
        fetch_server.serve(config)
        return 0
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
